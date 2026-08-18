"""
Crash-recovery experiment for atomic (state, trace) persistence
(reviewer concern R1-8).

Runs the default workload, then commits the step through the StepStore
protocol under four fault schedules:

  none         : no fault, pointer switched;
  after_trace  : crash after the trace file, before the state file;
  after_state  : crash after both artefacts, before the pointer switch;
  duplicate    : re-execution of the SAME (G_t, W_t) after a successful
                 commit (recovery-then-replay) -> refused by the
                 idempotence guard.

After every fault the recovery procedure runs and the consistency invariant
is checked: CURRENT always names a fully verified (state, trace) pair; a
step that crashed before the pointer switch is invisible (its staging is
discarded); the graph and the audit record never diverge.

Usage:
    python experiment_atomic_commit.py
Writes results/experiment_atomic_commit.json
"""
from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

from engine import run_engine
from persistence import CrashPoint, StepStore, graph_digest


def main():
    graph_t, _, _, successor, trace = run_engine(save_trace_file=False)
    input_digest = graph_digest(graph_t)
    window_id = trace["window"]["window_id"]
    slim_trace = {"summary": trace["summary"], "window_id": window_id}

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
    finally:
        shutil.rmtree(root, ignore_errors=True)

    for name, res in results.items():
        print(f"{name:<12} ->", json.dumps(res, default=str)[:160])

    out = Path(__file__).resolve().parent.parent / "results" / "experiment_atomic_commit.json"
    out.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    print("Wrote", out)
    return results


if __name__ == "__main__":
    main()
