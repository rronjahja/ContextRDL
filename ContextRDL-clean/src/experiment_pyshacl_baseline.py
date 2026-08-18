"""
Honest baseline experiment comparing three execution strategies on the same
input workloads.  Each strategy receives the same enabled action set produced
by the rule evaluator; they differ only in how they commit those actions.

  A. OURS             -- scheduled order + first-writer-wins + admissibility gate.
  B. SHACL_GATED      -- scheduled order + per-action pySHACL validation,
                         NO first-writer-wins (all non-conflicting writes apply).
  C. POSTHOC_RANDOM   -- apply all actions in random order with NO admissibility
                         gate; validate final graph once with pySHACL.

The point is that (A) is the only one that simultaneously produces a unique,
admissible, explained outcome.  (B) may be admissible but loses determinism
when multiple actions target the same (zone, predicate).  (C) often violates
constraints because nothing blocks unsafe final states.

For each workload we run each strategy 30 times and report:
  - unique successor digests
  - fraction of runs with admissible (SHACL-conforming) final state
  - mean accepted count

Results land in ``results/experiment_pyshacl_baseline.json``.
"""
from __future__ import annotations

import json
import os
import random
import statistics
import time
from copy import deepcopy
from typing import Any, Dict, List, Mapping

from rdflib import Graph, URIRef

from admissibility import check_admissibility_shacl
from dataset_builder import build_dataset, load_events, load_state
from experiment_helpers import governance_conflict_events, tie_conflict_events
from resolver import resolve_actions as resolve_original
from resolver_incremental import _graph_digest  # reuse digest helper
from rule_engine import (
    evaluate_rules,
    load_settings,
    resolve_governance_context,
    schedule_actions,
)
from rule_loader import load_rules
from state_transition import apply_action


WORKLOAD_RUNS = 30
SHAPES_PATH = "shapes/invariants.ttl"


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

def strategy_ours(base_graph: Graph, schedule: List[Dict], settings: Dict) -> Dict[str, Any]:
    accepted, successor, decisions = resolve_original(
        base_graph, schedule, shapes_path=SHAPES_PATH, settings=settings
    )
    return {
        "successor": successor,
        "accepted_count": len(accepted),
        "total": len(schedule),
    }


def strategy_shacl_gated_no_shadow(base_graph: Graph, enabled: List[Dict], settings: Dict) -> Dict[str, Any]:
    """
    Scheduled order, per-action pySHACL validation, NO first-writer-wins.
    Each action is applied to a candidate clone; on SHACL success we commit,
    otherwise we discard.  Two actions writing the same (zone,predicate) both
    apply: the later one overwrites the earlier one (last-writer-wins).
    """
    # Use the same schedule order the normal pipeline would produce.
    scheduled = schedule_actions(enabled, settings=settings)

    working = Graph()
    for t in base_graph:
        working.add(t)

    accepted = 0
    for action in scheduled:
        cand = apply_action(working, action)
        ok, _ = check_admissibility_shacl(cand, SHAPES_PATH)
        if ok:
            working = cand
            accepted += 1
    return {
        "successor": working,
        "accepted_count": accepted,
        "total": len(scheduled),
    }


def strategy_posthoc_random(base_graph: Graph, enabled: List[Dict]) -> Dict[str, Any]:
    """
    Random order, NO admissibility gate in-loop.  Apply every action blindly,
    then validate the final graph once with pySHACL.  Last-writer-wins per
    (zone, predicate) falls out of the overwrite semantics of apply_action.
    """
    order = list(enabled)
    random.shuffle(order)

    working = Graph()
    for t in base_graph:
        working.add(t)

    for action in order:
        working = apply_action(working, action)
    return {
        "successor": working,
        "accepted_count": len(order),
        "total": len(order),
    }


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

def _workload_enabled_and_schedule(events, settings_override=None):
    settings = load_settings("configs/settings.json")
    if settings_override:
        def deep_update(b, u):
            for k, v in u.items():
                if isinstance(v, dict) and isinstance(b.get(k), dict):
                    b[k] = deep_update(dict(b[k]), v)
                else:
                    b[k] = v
            return b
        settings = deep_update(settings, deepcopy(settings_override))
    context = resolve_governance_context(settings=settings, contexts_path="data/contexts.json")
    state = load_state("shapes/base_graph.ttl")
    rules = load_rules(settings.get("paths", {}).get("rules", "configs/rules.json"))
    dataset, meta = build_dataset(state, events, settings=settings)
    enabled = evaluate_rules(dataset, rules, settings=settings, context=context, window_meta=meta)
    schedule = schedule_actions(enabled, settings=settings)
    return state, enabled, schedule, settings


def _run_strategy_n_times(name: str, runner, runs: int) -> Dict[str, Any]:
    digests = set()
    admissible = 0
    accepted_counts = []
    times = []
    for _ in range(runs):
        t0 = time.perf_counter()
        result = runner()
        t1 = time.perf_counter()
        times.append(t1 - t0)
        digests.add(_graph_digest(result["successor"]))
        ok, _ = check_admissibility_shacl(result["successor"], SHAPES_PATH)
        if ok:
            admissible += 1
        accepted_counts.append(result["accepted_count"])
    return {
        "strategy": name,
        "runs": runs,
        "unique_successor_states": len(digests),
        "admissible_fraction": admissible / runs,
        "mean_accepted_count": statistics.mean(accepted_counts) if accepted_counts else 0,
        "mean_runtime_s": statistics.mean(times) if times else 0,
    }


def run_workload(label: str, events, settings_override=None) -> Dict[str, Any]:
    base, enabled, schedule, settings = _workload_enabled_and_schedule(events, settings_override)

    ours   = _run_strategy_n_times(
        "A_ours",
        lambda: strategy_ours(base, schedule, settings),
        WORKLOAD_RUNS,
    )
    shacl  = _run_strategy_n_times(
        "B_shacl_gated_no_shadow",
        lambda: strategy_shacl_gated_no_shadow(base, enabled, settings),
        WORKLOAD_RUNS,
    )
    posthoc = _run_strategy_n_times(
        "C_posthoc_random",
        lambda: strategy_posthoc_random(base, enabled),
        WORKLOAD_RUNS,
    )
    return {
        "workload":       label,
        "enabled_count":  len(enabled),
        "strategies":     [ours, shacl, posthoc],
    }


def main():
    random.seed(1)  # reproducible randomness for the posthoc strategy

    workloads = [
        ("default",              load_events("data/events.jsonl"), None),
        ("tie_conflict",         tie_conflict_events(), None),
        ("governance_conflict",  governance_conflict_events(), None),
    ]

    results = [run_workload(lbl, ev, ov) for lbl, ev, ov in workloads]

    os.makedirs("results", exist_ok=True)
    with open("results/experiment_pyshacl_baseline.json", "w", encoding="utf-8") as fh:
        json.dump({"workload_runs_each": WORKLOAD_RUNS, "workloads": results}, fh, indent=2)

    # Pretty print
    for w in results:
        print(f"\n== Workload: {w['workload']}  (enabled_count={w['enabled_count']}) ==")
        print(f"  {'strategy':30s} {'unique':>8s} {'admissible%':>12s} "
              f"{'meanAccepted':>13s} {'meanRt(ms)':>12s}")
        for s in w["strategies"]:
            print(f"  {s['strategy']:30s} {s['unique_successor_states']:>8d} "
                  f"{100*s['admissible_fraction']:>11.1f}% "
                  f"{s['mean_accepted_count']:>13.2f} "
                  f"{1000*s['mean_runtime_s']:>12.2f}")

    print("\nWrote results/experiment_pyshacl_baseline.json")


if __name__ == "__main__":
    main()
