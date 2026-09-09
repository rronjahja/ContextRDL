"""
Crash-recovery experiment for atomic (state, trace) persistence
(reviewer concern R1-8).

Runs the default workload, then commits the step through the StepStore
protocol under six fault schedules. The first four start from an empty store;
the last two start from a store in which step A is already committed and
inject the crash while step B (whose input graph is A's successor) is being
committed, so that CURRENT has a real pre-step value to preserve:

  none                   : no fault, pointer switched;
  after_trace            : crash after the trace file, before the state file;
  after_state            : crash after both artefacts, before the pointer switch;
  duplicate              : re-execution of the SAME (G_t, W_t) after a successful
                           commit (recovery-then-replay) -> refused by the
                           idempotence guard;
  committed_after_trace  : step A committed, crash on step B after its trace;
  committed_after_state  : step A committed, crash on step B after its state.
  In both committed_* schedules CURRENT must still name step A after
  recovery, A's artefacts must verify, and B's staging must be discarded.
  history                : three distinct steps A, B, C committed in sequence
                           (parent-linked chain); recovery must keep all three,
                           a re-commit of A must be refused, and a tampered
                           nested successor digest in C must fail verification.

The full trace of the step (not a summary) is the staged trace artefact.

After every fault the recovery procedure runs and the consistency invariant
is checked: CURRENT always names a fully verified (state, trace) pair; a
step that crashed before the pointer switch is invisible (its staging is
discarded); the graph and the audit record never diverge.

Usage:
    python experiment_atomic_commit.py
Writes results/hvac/experiment_atomic_commit.json
"""
from __future__ import annotations
import paths  # noqa: E402  (results layout)

import json
import shutil
import tempfile
from pathlib import Path

from engine import run_engine
from experiment_helpers import governance_conflict_events, tie_conflict_events
from persistence import CrashPoint, StepStore, graph_digest


def main():
    graph_t, _, _, successor, trace = run_engine(save_trace_file=False)
    input_digest = graph_digest(graph_t)
    window_id = trace["window"]["window_id"]
    slim_trace = dict(trace)  # the full trace is the staged artefact

    # Step B: the successor of step A is the input of the next step, evaluated
    # on the same event window (a different step identifier, since G_t differs).
    graph_b, _, _, successor_b, trace_b = run_engine(save_trace_file=False, state_graph=successor)
    input_digest_b = graph_digest(graph_b)
    window_id_b = trace_b["window"]["window_id"]

    results = {}
    root = Path(tempfile.mkdtemp(prefix="contextrdl_store_"))
    try:
        # 1) clean commit
        store = StepStore(root / "clean")
        step_id = store.commit_step(input_digest, window_id, successor, dict(slim_trace))
        results["none"] = {"committed": step_id, **store.verify_current()}

        # 2) crash after trace, before state
        store = StepStore(root / "after_trace")
        try:
            store.commit_step(input_digest, window_id, successor, dict(slim_trace),
                              crash_after="trace")
        except CrashPoint:
            pass
        recovery = store.recover()
        results["after_trace"] = {
            "current_after_crash": store.read_current(),
            "recovery": recovery,
            "invariant_holds": recovery["verification"]["consistent"]
            and store.read_current() is None,
        }

        # 3) crash after state, before pointer switch
        store = StepStore(root / "after_state")
        try:
            store.commit_step(input_digest, window_id, successor, dict(slim_trace),
                              crash_after="state")
        except CrashPoint:
            pass
        recovery = store.recover()
        results["after_state"] = {
            "current_after_crash": store.read_current(),
            "recovery": recovery,
            "invariant_holds": recovery["verification"]["consistent"]
            and store.read_current() is None,
        }

        # 4) duplicate replay after successful commit
        store = StepStore(root / "clean")
        try:
            store.commit_step(input_digest, window_id, successor, dict(slim_trace))
            results["duplicate"] = {"refused": False}
        except ValueError as exc:
            results["duplicate"] = {"refused": True, "message": str(exc)}

        # 5) and 6) step A committed, crash while committing step B
        for label, crash_after in (("committed_after_trace", "trace"),
                                   ("committed_after_state", "state")):
            store = StepStore(root / label)
            step_a = store.commit_step(input_digest, window_id, successor, dict(slim_trace))
            current_before = store.read_current()
            try:
                store.commit_step(input_digest_b, window_id_b, successor_b, dict(trace_b),
                                  crash_after=crash_after)
            except CrashPoint:
                pass
            recovery = store.recover()
            current_after = store.read_current()
            results[label] = {
                "committed_step_before_crash": step_a,
                "current_after_crash": current_after,
                "recovery": recovery,
                "invariant_holds": (
                    recovery["verification"]["consistent"]
                    and current_after == current_before
                    and current_after["step_id"] == step_a
                    and step_a not in recovery["discarded_incomplete_steps"]
                    and len(recovery["discarded_incomplete_steps"]) == 1
                ),
            }
        # 7) committed history survives recovery; stale and tampered records are refused
        store = StepStore(root / "history")
        chain, graph = [], None
        for events in (None, tie_conflict_events(), governance_conflict_events()):
            g_in, _, _, succ, tr = run_engine(save_trace_file=False, state_graph=graph, events=events)
            chain.append((store.commit_step(graph_digest(g_in), tr["window"]["window_id"], succ, dict(tr)),
                          g_in, succ, tr))
            graph = succ
        recovery = store.recover()
        step_a, g_a, succ_a, tr_a = chain[0]
        try:
            store.commit_step(graph_digest(g_a), tr_a["window"]["window_id"], succ_a, dict(tr_a))
            stale_refused = False
        except ValueError:
            stale_refused = True
        tampered = json.loads((store.steps_dir / chain[2][0] / "trace.json").read_text(encoding="utf-8"))
        tampered["successor_graph"]["digest"] = "0" * 64
        (store.steps_dir / chain[2][0] / "trace.json").write_text(json.dumps(tampered), encoding="utf-8")
        results["history"] = {
            "committed_steps": [c[0] for c in chain],
            "chain_after_recovery": recovery["committed_chain"],
            "discarded": recovery["discarded_incomplete_steps"],
            "all_three_retained": sorted(c[0] for c in chain) == sorted(recovery["committed_chain"]),
            "stale_recommit_refused": stale_refused,
            "tampered_nested_digest_detected": not store.verify_current()["consistent"],
            "invariant_holds": (recovery["verification"]["consistent"]
                                and sorted(c[0] for c in chain) == sorted(recovery["committed_chain"])
                                and not recovery["discarded_incomplete_steps"] and stale_refused
                                and not store.verify_current()["consistent"]),
        }
    finally:
        shutil.rmtree(root, ignore_errors=True)

    for name, res in results.items():
        print(f"{name:<12} ->", json.dumps(res, default=str)[:160])

    out = Path(paths.hvac("experiment_atomic_commit.json"))
    out.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    print("Wrote", out)
    if not all(v.get("invariant_holds", True) for v in results.values() if isinstance(v, dict)) \
            or not results["duplicate"].get("refused"):
        raise SystemExit("persistence invariant violated")
    return results


if __name__ == "__main__":
    main()
