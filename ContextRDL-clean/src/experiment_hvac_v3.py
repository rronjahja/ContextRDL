"""
HVAC v3 harness: produces every remaining number for the resubmission.

Four parts:
  1. Baseline+ : adds the DETERMINISTIC-ORDER (no admissibility) baseline to the
     existing three strategies, and reports mean +/- stdev runtime for ALL
     strategies, over three workloads (default, tie, governance). (R1.4, R1.6)
  2. R2.6 check: reports, for the SHACL-gated (no shadowing) strategy on the
     default workload, the ORDER of committed actions, so the manuscript can
     state whether the 6th committed action is r1 or r4. (R2.6)
  3. Scalability stdev: re-runs the two resolvers a few times per N to report
     mean +/- stdev. (R1.6)  [optional/quick: small repeat count]
  4. Replay table: generates a trace per workload (default, tie, governance op>occ,
     governance occ>op, stress N=64) and runs full replay on each, reporting the
     per-field match counts for the replay table. (R1.7)
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
from trace import graph_digest  # noqa: E402

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
    state = load_state("shapes/base_graph.ttl")
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
    order = list(enabled)
    random.shuffle(order)
    working = Graph()
    for t in state:
        working.add(t)
    for a in order:
        working = apply_action(working, a)
    return working, len(order)


def measure(name, runner, runs=RUNS):
    digests, admissible, accepted, times = set(), 0, [], []
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
    }


def run_workload(label, events, override=None):
    state, enabled, schedule, settings = pipeline(events, override)
    return {
        "workload": label,
        "enabled_count": len(enabled),
        "strategies": [
            measure("Ours", lambda: strat_ours(state, schedule, settings)),
            measure("SHACL-gated, no shadowing", lambda: strat_shacl_gated(state, enabled, settings)),
            measure("Deterministic-order, no admissibility",
                    lambda: strat_deterministic_no_adm(state, enabled, settings)),
            measure("Random-order, no admissibility", lambda: strat_random_no_adm(state, enabled)),
        ],
    }


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
        ("governance (occ > occ reversed)",
         dict(events=governance_conflict_events(),
              settings_override={"role_precedence": {"occupant": 0, "operator": 1, "emergency": 2}})),
    ]
    rows = {}
    for label, kw in jobs:
        tp = f"results/trace_{label.split()[0]}_{abs(hash(label)) % 10000}.json"
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
    random.seed(1)
    os.makedirs("results", exist_ok=True)

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
        "runs_per_strategy": RUNS,
        "baseline": baseline,
        "r26_committed_order": r26,
        "replay": replay,
        "replay_error": replay_error,
    }
    with open("results/experiment_hvac_v3.json", "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2)

    # ---- pretty print ----
    print("=" * 72)
    print("PART 1: BASELINE (4 strategies, mean +/- sd runtime)")
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

    print("\nWrote results/experiment_hvac_v3.json")


if __name__ == "__main__":
    main()