"""
Controlled runtime comparison (reviewer concern R1-4).

The earlier Table 6 compared "ours" (incremental validator + shadowing)
against "SHACL-gated, no shadowing" (full pySHACL + no shadowing), so the
reported ~50x difference confounded (a) the conflict gate and (b) the
validator implementation. This experiment isolates the factors in a 2x2
design, holding EVERYTHING else fixed (same resolver code path, same
clone-per-action graph handling, same digesting, same schedule):

    Factor A  conflict gate : first-writer-wins ON | OFF
    Factor B  validator     : full pySHACL         | incremental (full-graph)

plus a phase-level wall-clock breakdown (enablement, action construction,
scheduling, policy guard, conflict gate, validation, mutation+digest) for
the two full configurations.

Usage:
    python experiment_runtime_controlled.py [trials]
Writes results/experiment_runtime_controlled.json
"""
from __future__ import annotations

import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Tuple

from rdflib import Graph

from admissibility import check_admissibility_incremental, check_admissibility_shacl
from dataset_builder import build_dataset, load_events, load_state
from resolver import check_policy_guard
from rule_engine import evaluate_rules, load_settings, resolve_governance_context, schedule_actions
from rule_loader import load_rules
from state_transition import apply_action
from trace import graph_digest

SHAPES = "shapes/invariants.ttl"


def _clone(graph: Graph) -> Graph:
    g = Graph()
    for t in graph:
        g.add(t)
    return g


def resolve_controlled(
    graph: Graph,
    schedule: List[Mapping[str, Any]],
    validator: Callable[[Graph], Tuple[bool, str]],
    conflict_gate: bool,
    phases: Dict[str, float] | None = None,
) -> Tuple[List[Dict[str, Any]], Graph, int]:
    """Reference resolution loop with injectable validator and toggleable
    conflict gate. Identical graph handling (clone per action) in all cells."""

    def tick(name: str, t0: float):
        if phases is not None:
            phases[name] = phases.get(name, 0.0) + (time.perf_counter() - t0)

    accepted: List[Dict[str, Any]] = []
    current = _clone(graph)
    accepted_targets: set[str] = set()
    validations = 0

    for action in schedule:
        target_key = str(action["target_key"])

        t0 = time.perf_counter()
        shadowed = conflict_gate and target_key in accepted_targets
        tick("conflict_gate", t0)
        if shadowed:
            continue

        t0 = time.perf_counter()
        policy_ok, _ = check_policy_guard(current, action)
        tick("policy_guard", t0)
        if not policy_ok:
            continue

        t0 = time.perf_counter()
        candidate = apply_action(current, action)
        tick("mutation", t0)

        t0 = time.perf_counter()
        conforms, _ = validator(candidate)
        validations += 1
        tick("validation", t0)

        if conforms:
            current = candidate
            accepted_targets.add(target_key)
            accepted.append(dict(action))

    t0 = time.perf_counter()
    digest = graph_digest(current)
    tick("digest", t0)
    return accepted, current, validations


def build_default_schedule():
    settings = load_settings("configs/settings.json")
    context = resolve_governance_context(settings=settings, contexts_path="data/contexts.json")
    graph_t = load_state("shapes/base_graph.ttl")
    events = load_events("data/events.jsonl")
    rules = load_rules("configs/rules.json")
    dataset, window_meta = build_dataset(graph_t, events, settings=settings)
    window_meta = dict(window_meta)
    window_meta["governance_context"] = context
    enabled = evaluate_rules(dataset, rules, settings=settings, context=context, window_meta=window_meta)
    schedule = schedule_actions(enabled, settings=settings)
    return graph_t, schedule


VALIDATORS = {
    "full_shacl": lambda g: check_admissibility_shacl(g, SHAPES),
    "incremental": lambda g: check_admissibility_incremental(g),
}


def main(trials: int = 30):
    graph_t, schedule = build_default_schedule()

    cells = []
    for gate in (True, False):
        for vname, vfn in VALIDATORS.items():
            # warm-up (shape-graph cache, imports)
            resolve_controlled(graph_t, schedule, vfn, gate)
            times, committed, digests, validations = [], [], set(), []
            for _ in range(trials):
                t0 = time.perf_counter()
                acc, succ, nval = resolve_controlled(graph_t, schedule, vfn, gate)
                times.append((time.perf_counter() - t0) * 1000.0)
                committed.append(len(acc))
                validations.append(nval)
                digests.add(graph_digest(succ))
            final_ok, _ = check_admissibility_shacl(succ, SHAPES)
            cells.append({
                "conflict_gate": "first_writer_wins" if gate else "off",
                "validator": vname,
                "trials": trials,
                "mean_ms": round(statistics.mean(times), 2),
                "sd_ms": round(statistics.stdev(times), 2),
                "mean_committed": statistics.mean(committed),
                "mean_validation_calls": statistics.mean(validations),
                "unique_states": len(digests),
                "final_admissible": final_ok,
            })
            c = cells[-1]
            print(f"gate={c['conflict_gate']:<17} validator={vname:<11} "
                  f"{c['mean_ms']:8.2f} ± {c['sd_ms']:.2f} ms  "
                  f"committed={c['mean_committed']}  valcalls={c['mean_validation_calls']}  "
                  f"admissible={c['final_admissible']}")

    # Phase-level breakdown for the two full configurations (gate ON)
    breakdown = {}
    for vname, vfn in VALIDATORS.items():
        phases: Dict[str, float] = {}
        for _ in range(trials):
            resolve_controlled(graph_t, schedule, vfn, True, phases=phases)
        breakdown[vname] = {k: round(v * 1000.0 / trials, 3) for k, v in sorted(phases.items())}
        print(f"phase breakdown ({vname}, per step, ms):", breakdown[vname])

    out = Path(__file__).resolve().parent.parent / "results" / "experiment_runtime_controlled.json"
    out.write_text(json.dumps({"cells": cells, "phase_breakdown_ms": breakdown}, indent=2), encoding="utf-8")
    print("Wrote", out)


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 30)
