"""
Scalability comparison.

The original scalability experiment reports O(N^2) wall-clock from the
original resolver.  Most of that cost is implementation, not semantics:
every action clones the full graph, computes two full digests, runs a
full-graph admissibility check, and diffs both graphs.  With N zones and
N actions this is O(N) per action, O(N^2) total.

This experiment runs both resolvers on identical synthetic workloads and:
  * asserts that the outcome is identical (same accepted count,
    same successor digest), which is the prerequisite for any perf claim;
  * records wall-clock times for both;
  * writes a CSV + JSON summary for plotting.

Workload: N zones, each receives one PreheatRequest event.  All N enabled
actions are admissible in isolation and target distinct (zone, predicate).
Nothing is shadowed, nothing is rejected -- this is the pure "per-action
overhead" case the original scalability plot measured.
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import time
from typing import Any, Dict, List

from rdflib import Graph, Literal, Namespace
from rdflib.namespace import RDF, XSD

from dataset_builder import build_dataset
from resolver import resolve_actions as resolve_original
from resolver_incremental import resolve_actions_incremental
from rule_engine import (
    evaluate_rules,
    load_settings,
    resolve_governance_context,
    schedule_actions,
)
from rule_loader import load_rules


EX = Namespace("http://example.org/building#")

DEFAULT_SIZES = [10, 50, 100, 200, 400, 800]


def _graph_digest(graph: Graph) -> str:
    lines = sorted(f"{s.n3()} {p.n3()} {o.n3()} ." for s, p, o in graph)
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def build_synthetic_state(num_zones: int) -> Graph:
    g = Graph()
    g.bind("ex", EX)
    for idx in range(num_zones):
        zone = EX[f"ScaleZone{idx:05d}"]
        g.add((zone, RDF.type, EX.HVAC_Zone))
        g.add((zone, EX.currentSetpoint, Literal(20.0, datatype=XSD.decimal)))
        g.add((zone, EX.occupied, Literal(False, datatype=XSD.boolean)))
        g.add((zone, EX.co2Level, Literal(500.0, datatype=XSD.decimal)))
        g.add((zone, EX.ventilationMode, Literal("normal")))
        g.add((zone, EX.emergencyState, Literal(False, datatype=XSD.boolean)))

    g.add((EX.Policy, RDF.type, EX.ControlPolicy))
    g.add((EX.Policy, EX.occupantMaxSetpoint,  Literal(23.0, datatype=XSD.decimal)))
    g.add((EX.Policy, EX.operatorMaxSetpoint,  Literal(24.0, datatype=XSD.decimal)))
    g.add((EX.Policy, EX.emergencyMaxSetpoint, Literal(26.0, datatype=XSD.decimal)))
    g.add((EX.Policy, EX.minSetpoint,          Literal(18.0, datatype=XSD.decimal)))
    return g


def build_synthetic_events(num_actions: int) -> List[Dict[str, Any]]:
    ts = "2026-03-14T09:00:00Z"
    return [
        {
            "eid": f"scale-preheat-{idx:05d}",
            "timestamp": ts,
            "type": "PreheatRequest",
            "role": "operator",
            "payload": {"zone": str(EX[f"ScaleZone{idx:05d}"]), "target": 22},
        }
        for idx in range(num_actions)
    ]


def run_case(num_actions: int) -> Dict[str, Any]:
    settings = load_settings("configs/settings.json")
    context  = resolve_governance_context(settings=settings, contexts_path="data/contexts.json")
    rules    = load_rules(settings.get("paths", {}).get("rules", "configs/rules.json"))

    state = build_synthetic_state(num_actions)
    events = build_synthetic_events(num_actions)
    dataset, meta = build_dataset(state, events, settings=settings)

    enabled = evaluate_rules(dataset, rules, settings=settings, context=context, window_meta=meta)
    schedule = schedule_actions(enabled, settings=settings)

    # --- Original resolver ---
    t0 = time.perf_counter()
    accepted_o, successor_o, _ = resolve_original(
        state, schedule, shapes_path="shapes/invariants.ttl", settings=settings
    )
    t_orig = time.perf_counter() - t0
    digest_o = _graph_digest(successor_o)

    # --- Incremental resolver ---
    state2 = Graph()
    for t in state:
        state2.add(t)
    t0 = time.perf_counter()
    accepted_n, successor_n, _ = resolve_actions_incremental(
        state2, schedule, shapes_path="shapes/invariants.ttl",
        settings=settings, record_digests=False,   # fast path
    )
    t_new = time.perf_counter() - t0
    digest_n = _graph_digest(successor_n)

    return {
        "actions":              len(enabled),
        "accepted_orig":        len(accepted_o),
        "accepted_new":         len(accepted_n),
        "accepted_match":       len(accepted_o) == len(accepted_n),
        "digest_match":         digest_o == digest_n,
        "successor_digest":     digest_o,
        "time_seconds_orig":    round(t_orig, 6),
        "time_seconds_new":     round(t_new, 6),
        "speedup_x":            round(t_orig / t_new, 2) if t_new > 0 else None,
    }


def main(sizes=None):
    sizes = sizes or DEFAULT_SIZES
    rows = []
    for size in sizes:
        row = run_case(size)
        rows.append(row)
        match = "OK" if (row["accepted_match"] and row["digest_match"]) else "MISMATCH"
        print(
            f"N={row['actions']:>5d}  orig={row['time_seconds_orig']:>8.3f}s  "
            f"new={row['time_seconds_new']:>8.3f}s  speedup={row['speedup_x']}x  [{match}]"
        )

    os.makedirs("results", exist_ok=True)
    with open("results/experiment_scalability_v2.csv", "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    with open("results/experiment_scalability_v2.json", "w", encoding="utf-8") as fh:
        json.dump({"rows": rows}, fh, indent=2)

    print("\nWrote results/experiment_scalability_v2.csv and .json")

    mismatches = [r for r in rows if not (r["accepted_match"] and r["digest_match"])]
    if mismatches:
        print(f"WARNING: {len(mismatches)} size(s) produced different outcomes "
              f"between resolvers.  Do not trust timing until this is fixed.")


if __name__ == "__main__":
    main()
