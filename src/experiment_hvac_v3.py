"""HVAC comparisons with explicit order, transaction and validation controls.

Measure seven strategies over default, tie and governance workloads, retaining
raw timings and successor digests. Check per-event and per-action arrival-order
relations against the configured resolver, report committed-action order for
the SHACL-only ablation, and generate workload traces for full pipeline replay.
The dedicated scalability harnesses supply the separate scalability tables.
"""
from __future__ import annotations

import json
import os
import random
import statistics
import sys
import time
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, List, Mapping

# Make project imports work whether run from root or from src/
HERE = Path(__file__).resolve().parent
if (HERE / "rule_engine.py").exists():
    sys.path.insert(0, str(HERE))
    PROJECT_ROOT = HERE.parent
else:
    PROJECT_ROOT = HERE
import paths  # noqa: E402  (results layout)
os.chdir(PROJECT_ROOT)  # so relative data paths resolve like the other experiments

from rdflib import Graph  # noqa: E402

from admissibility import check_admissibility_shacl  # noqa: E402
from dataset_builder import build_dataset, load_events, load_state  # noqa: E402
from experiment_helpers import governance_conflict_events, tie_conflict_events  # noqa: E402
from resolver import resolve_actions as resolve_original  # noqa: E402
from rule_engine import (  # noqa: E402
    evaluate_rules, load_settings, resolve_governance_context, schedule_actions,
)
from rule_loader import load_rules  # noqa: E402
from state_transition import apply_action  # noqa: E402
from trace import graph_digest, _environment  # noqa: E402
from policy_profile import validate_hvac_policy

SHAPES = "shapes/invariants.ttl"
RUNS = 30


# ---------------------------------------------------------------------------
# Pipeline helper
# ---------------------------------------------------------------------------

def _deep_update(b, u):
    for k, v in u.items():
        if isinstance(v, dict) and isinstance(b.get(k), dict):
            b[k] = _deep_update(dict(b[k]), v)
        else:
            b[k] = v
    return b


def pipeline(events, settings_override=None, context_name=None):
    settings = load_settings("configs/settings.json")
    if settings_override:
        settings = _deep_update(settings, deepcopy(settings_override))
    context = resolve_governance_context(settings=settings, contexts_path="data/contexts.json",
                                         context_name=context_name)
    state = load_state("data/base_graph.ttl")
    validate_hvac_policy(state)
    settings.setdefault("governance", {})["active_roles"] = context["active_roles"]
    rules = load_rules("configs/rules.json")
    dataset, meta = build_dataset(state, events, settings=settings)
    enabled = evaluate_rules(dataset, rules, settings=settings, context=context, window_meta=meta)
    schedule = schedule_actions(enabled, settings=settings)
    return state, enabled, schedule, settings


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

def strat_ours(state, schedule, settings):
    acc, succ, _ = resolve_original(state, schedule, shapes_path=SHAPES, settings=settings)
    return succ, len(acc)


def strat_shacl_gated(state, enabled, settings):
    sched = schedule_actions(enabled, settings=settings)
    working = Graph()
    for t in state:
        working.add(t)
    acc = 0
    for a in sched:
        cand = apply_action(working, a)
        ok, _ = check_admissibility_shacl(cand, SHAPES)
        if ok:
            working = cand
            acc += 1
    return working, acc


def strat_deterministic_no_adm(state, enabled, settings):
    """Fixed schedule order, NO conflict gate, NO admissibility. Validate once at end."""
    sched = schedule_actions(enabled, settings=settings)
    working = Graph()
    for t in state:
        working.add(t)
    for a in sched:
        working = apply_action(working, a)
    return working, len(sched)


def strat_random_no_adm(state, enabled):
    # Canonical starting order makes the seeded finite sample reproducible
    # independently of the SPARQL result iterator.
    order = sorted(enabled, key=lambda action: action["aid"])
    random.shuffle(order)
    working = Graph()
    for t in state:
        working.add(t)
    for a in order:
        working = apply_action(working, a)
    return working, len(order)


def strat_arrival_validated(state, enabled, events, per_event):
    """Ingestion order; validate one action or all writes from one event.

    Query enablement and snapshot payloads are shared. These strategies omit
    runtime role, numeric-policy and first-writer-wins gates. An invalid unit
    is discarded in its entirety. No deployed RDF store is invoked here.
    """
    ingestion = {}
    for index, event in enumerate(events):
        ingestion.setdefault(event["eid"], index)
    ordered = sorted(enabled, key=lambda action: (ingestion[action["event_id"]],
                     action["rid"], action["bindKey"], action["aid"]))
    units = []
    for action in ordered:
        if per_event and units and units[-1][0]["event_id"] == action["event_id"]:
            units[-1].append(action)
        else:
            units.append([action])
    working, accepted = state, 0
    for unit in units:
        candidate = working
        for action in unit:
            candidate = apply_action(candidate, action)
        if check_admissibility_shacl(candidate, SHAPES)[0]:
            working, accepted = candidate, accepted + len(unit)
    return working, accepted


def strat_shuffled_four_gates(state, schedule, settings, rng):
    ordered = list(schedule)
    rng.shuffle(ordered)
    return strat_ours(state, ordered, settings)


def measure(name, runner, runs=RUNS):
    digests, admissible, accepted, times = set(), 0, [], []
    rng_state = random.getstate()
    for _ in range(2):
        runner()
    random.setstate(rng_state)
    for _ in range(runs):
        t0 = time.perf_counter()
        succ, acc = runner()
        times.append(time.perf_counter() - t0)
        digests.add(graph_digest(succ))
        ok, _ = check_admissibility_shacl(succ, SHAPES)
        if ok:
            admissible += 1
        accepted.append(acc)
    return {
        "strategy": name,
        "unique_states": len(digests),
        "admissible_pct": round(100.0 * admissible / runs, 1),
        "mean_committed": round(statistics.mean(accepted), 1),
        "mean_runtime_ms": round(1000.0 * statistics.mean(times), 2),
        "sd_runtime_ms": round(1000.0 * (statistics.stdev(times) if len(times) > 1 else 0.0), 2),
        "runtime_samples_ms": [1000.0 * value for value in times],
        "warmup_runs": 2,
    }


def run_workload(label, events, override=None):
    state, enabled, schedule, settings = pipeline(events, override)
    order_rng = random.Random(20260910)
    return {
        "workload": label,
        "enabled_count": len(enabled),
        "strategies": [
            measure("Ours", lambda: strat_ours(state, schedule, settings)),
            measure("SHACL only, fixed order", lambda: strat_shacl_gated(state, enabled, settings)),
            measure("Fixed order, no gates",
                    lambda: strat_deterministic_no_adm(state, enabled, settings)),
            measure("Shuffled order, no gates", lambda: strat_random_no_adm(state, enabled)),
            measure("Arrival order, per-action SHACL", lambda: strat_arrival_validated(state, enabled, events, False)),
            measure("Arrival order, per-event SHACL", lambda: strat_arrival_validated(state, enabled, events, True)),
            measure("Shuffled order, all four gates", lambda: strat_shuffled_four_gates(state, schedule, settings, order_rng)),
        ],
    }


def transaction_comparison():
    fixtures = [
        ("default", load_events("data/events.jsonl"), None, True, False),
        ("tie conflict", tie_conflict_events(), None, False, False),
        ("governance op > occ", governance_conflict_events(), None, True, True),
        ("governance occ > op", governance_conflict_events(),
         {"role_precedence": {"emergency": 0, "occupant": 1, "operator": 2}}, False, False),
    ]
    rows = []
    for label, events, override, event_same, action_same in fixtures:
        state, enabled, schedule, settings = pipeline(events, override)
        successor, count = strat_ours(state, schedule, settings)
        reference_digest = graph_digest(successor)
        row = {"workload": label, "resolver_digest": reference_digest, "resolver_committed": count}
        for name, grouped, expected in (("per_event", True, event_same), ("per_action", False, action_same)):
            candidate, count = strat_arrival_validated(state, enabled, events, grouped)
            digest = graph_digest(candidate)
            assert (digest == reference_digest) == expected, (label, name)
            assert check_admissibility_shacl(candidate, SHAPES)[0]
            row[name] = {"digest": digest, "matches_resolver": digest == reference_digest,
                         "committed": count, "final_admissible": True}
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# Part 2: R2.6 -- order of committed actions under SHACL-gated on default
# ---------------------------------------------------------------------------

def r26_committed_order():
    state, enabled, _, settings = pipeline(load_events("data/events.jsonl"))
    sched = schedule_actions(enabled, settings=settings)
    working = Graph()
    for t in state:
        working.add(t)
    committed = []
    for a in sched:
        cand = apply_action(working, a)
        ok, _ = check_admissibility_shacl(cand, SHAPES)
        if ok:
            working = cand
            committed.append({
                "rid": a["rid"],
                "zone": a["zone"].split("#")[-1],
                "predicate": a["predicate"].split("#")[-1],
                "value": a["value"],
            })
    return {"committed_in_order": committed,
            "sixth_committed_rid": committed[5]["rid"] if len(committed) >= 6 else None}


# ---------------------------------------------------------------------------
# Part 4: per-workload replay
# ---------------------------------------------------------------------------

def run_replay_table():
    from engine import run_engine
    from replay_full import replay_full

    jobs = [
        ("default", dict(events_path="data/events.jsonl")),
        ("tie conflict", dict(events=tie_conflict_events())),
        ("governance (op > occ)", dict(events=governance_conflict_events())),
        ("governance (occ > op)",
         dict(events=governance_conflict_events(),
              settings_override={"role_precedence": {"occupant": 0, "operator": 1, "emergency": 2}})),
    ]
    trace_names = {
        "default": "trace_default.json",
        "tie conflict": "trace_tie_conflict.json",
        "governance (op > occ)": "trace_governance_op_gt_occ.json",
        "governance (occ > op)": "trace_governance_occ_gt_op.json",
    }
    rows = {}
    for label, kw in jobs:
        # Deterministic file names. (An earlier revision derived the suffix from
        # Python's per-process hash(), which produced a new file name on every run.)
        tp = paths.hvac_trace(trace_names[label])
        run_engine(trace_path=tp, save_trace_file=True, **kw)
        rep = replay_full(trace_path=tp)
        n_enabled = rep.get("regenerated", {}).get("enabled_count")
        rows[label] = {
            "enabled_match": rep["enabled_match"],
            "schedule_match": rep["schedule_match"],
            "accepted_match": rep["accepted_match"],
            "decisions_match": rep["decisions_match"],
            "digest_match": rep["digest_match"],
            "overall_pass": rep["overall_pass"],
            "enabled_count": n_enabled,
        }
    return rows


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    os.environ["ADMISSIBILITY_REGIME"] = "shacl"
    random.seed(1)
    paths.ensure_dirs()

    workloads = [
        ("default", load_events("data/events.jsonl"), None),
        ("tie conflict", tie_conflict_events(), None),
        ("governance conflict", governance_conflict_events(), None),
    ]
    baseline = [run_workload(lbl, ev, ov) for lbl, ev, ov in workloads]
    r26 = r26_committed_order()

    try:
        replay = run_replay_table()
        replay_error = None
    except Exception as e:  # replay is the most fragile part; never lose the rest
        replay = {}
        replay_error = f"{type(e).__name__}: {e}"

    summary = {
        "environment": _environment(),
        "admissibility_regime": "shacl",
        "runs_per_strategy": RUNS,
        "baseline": baseline,
        "r26_committed_order": r26,
        "replay": replay,
        "replay_error": replay_error,
        "transaction_comparison": transaction_comparison(),
    }
    with open(paths.hvac("experiment_hvac_v3.json"), "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2, default=str)

    # ---- pretty print ----
    print("=" * 72)
    print("PART 1: BASELINE (7 strategies, mean +/- sd runtime)")
    print("=" * 72)
    for w in baseline:
        print(f"\n-- {w['workload']} (enabled={w['enabled_count']}) --")
        print(f"  {'strategy':40s} {'uniq':>4s} {'adm%':>6s} {'committed':>9s} {'runtime ms':>14s}")
        for s in w["strategies"]:
            print(f"  {s['strategy']:40s} {s['unique_states']:>4d} {s['admissible_pct']:>6.1f} "
                  f"{s['mean_committed']:>9.1f} {s['mean_runtime_ms']:>8.2f}+-{s['sd_runtime_ms']:.2f}")

    print("\n" + "=" * 72)
    print("PART 2: R2.6 -- committed order under SHACL-gated (default workload)")
    print("=" * 72)
    for i, c in enumerate(r26["committed_in_order"], 1):
        print(f"  {i}. {c['rid']:4s} {c['zone']:6s} {c['predicate']:16s} = {c['value']}")
    print(f"  >>> SIXTH committed action is: {r26['sixth_committed_rid']}")

    print("\n" + "=" * 72)
    print("PART 3: REPLAY TABLE")
    print("=" * 72)
    if replay_error:
        print("  replay error:", replay_error)
    else:
        for label, r in replay.items():
            flags = " ".join(f"{k.split('_')[0]}={'OK' if v else 'FAIL'}"
                              for k, v in r.items() if k.endswith("_match"))
            print(f"  {label:34s} enabled={r['enabled_count']}  {flags}  overall={'PASS' if r['overall_pass'] else 'FAIL'}")

    print("\nWrote", paths.relative(paths.hvac("experiment_hvac_v3.json")))
    expected_rows = {"default", "tie conflict", "governance (op > occ)", "governance (occ > op)"}
    ours_ok = all(s["unique_states"] == 1 and s["admissible_pct"] == 100.0
                  for w in summary["baseline"] for s in w["strategies"] if s["strategy"] == "Ours")
    validated_ok = all(s["admissible_pct"] == 100.0 for w in summary["baseline"] for s in w["strategies"]
                       if s["strategy"] not in {"Fixed order, no gates", "Shuffled order, no gates"})
    replay_ok = (not replay_error and set(replay) >= expected_rows
                 and all(r["overall_pass"] for r in replay.values()))
    if not (ours_ok and replay_ok and validated_ok):
        raise SystemExit("experiment_hvac_v3: a correctness check failed (see results/hvac/experiment_hvac_v3.json)")


if __name__ == "__main__":
    main()
