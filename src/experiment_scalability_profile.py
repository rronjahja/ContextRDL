"""
Scalability profiling (reviewer concern R1-6).

Extends the wall-clock scalability table with the quantities the runtime
numbers must be conditioned on:

  A) Accepted-only workload (as before, self-contained here so the table
     can be regenerated from the public repository alone), reporting per N:
     graph triples, enabled/accepted/rejected counts, wall-clock for the
     incremental resolver, peak Python heap (tracemalloc), and the size of
     the recorded trace JSON.

  B) Mixed-rejection workload: 50% of the synthetic requests violate the
     comfort/policy caps, so the rejected path (undo / no-mutation) is
     exercised at scale.

  C) Cross-target workload: one feeder-budget SPARQL shape couples all
     synthetic charging points (EV-style), so zone-local validation is not
     applicable and the reference pySHACL validator is used per action.
     This measures how the general (non-target-local) case scales and makes
     explicit that the headline 55 ms figure belongs to the zone-local
     class only.

Usage:
    python experiment_scalability_profile.py [--sizes 100,200,400,800] [--repeats 3]
Writes results/experiment_scalability_profile.json
"""
from __future__ import annotations

import argparse
import json
import statistics
import time
import tracemalloc
from pathlib import Path
from typing import Dict, List

from rdflib import Graph, Literal, Namespace
from rdflib.namespace import RDF, XSD

from dataset_builder import build_dataset
from resolver_incremental import resolve_actions_incremental
from rule_engine import evaluate_rules, load_settings, resolve_governance_context, schedule_actions
from rule_loader import load_rules
from trace import build_trace, graph_digest

EX = Namespace("http://example.org/building#")
EV = Namespace("http://example.org/ev#")


# ---------- workload A/B: HVAC-style, zone-local shapes ----------------------

def build_state(num_zones: int) -> Graph:
    g = Graph()
    g.bind("ex", EX)
    for idx in range(num_zones):
        zone = EX[f"ScaleZone{idx:05d}"]
        g.add((zone, RDF.type, EX.HVAC_Zone))
        g.add((zone, EX.currentSetpoint, Literal(20.0, datatype=XSD.decimal)))
        g.add((zone, EX.ventilationMode, Literal("normal")))
        g.add((zone, EX.emergencyState, Literal(False, datatype=XSD.boolean)))
    g.add((EX.Policy, RDF.type, EX.ControlPolicy))
    g.add((EX.Policy, EX.occupantMaxSetpoint, Literal(23.0, datatype=XSD.decimal)))
    g.add((EX.Policy, EX.operatorMaxSetpoint, Literal(24.0, datatype=XSD.decimal)))
    g.add((EX.Policy, EX.emergencyMaxSetpoint, Literal(26.0, datatype=XSD.decimal)))
    g.add((EX.Policy, EX.minSetpoint, Literal(18.0, datatype=XSD.decimal)))
    return g


def build_events(num_actions: int, reject_fraction: float = 0.0) -> List[Dict[str, object]]:
    events: List[Dict[str, object]] = []
    n_reject = int(num_actions * reject_fraction)
    for idx in range(num_actions):
        # Rejected requests propose 27 C: above the operator policy cap (24),
        # so they fail the policy guard; the zone shape cap (26) backs it up.
        target = 27 if idx < n_reject else 22
        events.append({
            "eid": f"scale-preheat-{idx:05d}",
            "timestamp": "2026-03-14T09:00:00Z",
            "type": "PreheatRequest",
            "role": "operator",
            "payload": {"zone": str(EX[f"ScaleZone{idx:05d}"]), "target": target},
        })
    return events


def run_local_case(num_actions: int, reject_fraction: float, repeats: int) -> Dict[str, object]:
    settings = load_settings("configs/settings.json")
    context = resolve_governance_context(settings=settings, contexts_path="data/contexts.json")
    rules = load_rules("configs/rules.json")

    graph_t = build_state(num_actions)
    events = build_events(num_actions, reject_fraction)
    dataset, window_meta = build_dataset(graph_t, events, settings=settings)
    window_meta = dict(window_meta)
    window_meta["governance_context"] = context
    enabled = evaluate_rules(dataset, rules, settings=settings, context=context, window_meta=window_meta)
    schedule = schedule_actions(enabled, settings=settings)

    times, peaks = [], []
    for _ in range(repeats):
        tracemalloc.start()
        t0 = time.perf_counter()
        accepted, successor, decisions = resolve_actions_incremental(
            graph_t, schedule, shapes_path="shapes/invariants.ttl",
            settings=settings, record_digests=False)
        times.append(time.perf_counter() - t0)
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        peaks.append(peak)

    # trace size with full digests, once
    accepted, successor, decisions = resolve_actions_incremental(
        graph_t, schedule, shapes_path="shapes/invariants.ttl",
        settings=settings, record_digests=True)
    trace = build_trace(input_graph=graph_t, enabled_actions=enabled, schedule=schedule,
                        accepted_actions=accepted, successor_graph=successor,
                        decisions=decisions, settings=settings, window_meta=window_meta,
                        rules=rules, events=events)
    trace_bytes = len(json.dumps(trace, default=str).encode("utf-8"))

    return {
        "N": num_actions,
        "reject_fraction": reject_fraction,
        "graph_triples": len(graph_t),
        "enabled": len(enabled),
        "accepted": len(accepted),
        "rejected": len(enabled) - len(accepted),
        "incr_mean_s": round(statistics.mean(times), 4),
        "incr_sd_s": round(statistics.stdev(times), 4) if repeats > 1 else 0.0,
        "peak_heap_mib": round(max(peaks) / (1024 * 1024), 2),
        "trace_kib": round(trace_bytes / 1024, 1),
        "successor_digest": graph_digest(successor)[:16],
    }


# ---------- workload C: cross-target feeder budget, reference validator ------

CROSS_SHAPES_TEMPLATE = """@prefix sh: <http://www.w3.org/ns/shacl#> .
@prefix ev: <http://example.org/ev#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .

ev:PointPowerShape a sh:NodeShape ;
    sh:targetClass ev:ChargingPoint ;
    sh:property [
        sh:path ev:chargingPower ;
        sh:minCount 1 ; sh:maxCount 1 ;
        sh:minInclusive 0.0 ; sh:maxInclusive 22.0 ;
    ] .

ev:FeederBudgetShape a sh:NodeShape ;
    sh:targetNode ev:Feeder1 ;
    sh:sparql [
        sh:select \"\"\"
            SELECT $this WHERE {
                { SELECT (SUM(?p) AS ?total) WHERE {
                    ?cp a <http://example.org/ev#ChargingPoint> ;
                        <http://example.org/ev#chargingPower> ?p . } }
                $this <http://example.org/ev#feederBudget> ?b .
                FILTER (?total > ?b)
            } \"\"\" ;
    ] .
"""


def run_cross_target_case(num_points: int, repeats: int) -> Dict[str, object]:
    from pyshacl import validate

    shapes = Graph()
    shapes.parse(data=CROSS_SHAPES_TEMPLATE, format="turtle")

    def fresh_state() -> Graph:
        g = Graph()
        g.bind("ev", EV)
        for idx in range(num_points):
            cp = EV[f"CP{idx:05d}"]
            g.add((cp, RDF.type, EV.ChargingPoint))
            g.add((cp, EV.chargingPower, Literal(0.0, datatype=XSD.decimal)))
        # budget admits roughly half of the 22 kW requests
        budget = 11.0 * num_points
        g.add((EV.Feeder1, EV.feederBudget, Literal(budget, datatype=XSD.decimal)))
        return g

    times, accepted_counts = [], []
    for _ in range(repeats):
        g = fresh_state()
        accepted = 0
        t0 = time.perf_counter()
        for idx in range(num_points):
            cp = EV[f"CP{idx:05d}"]
            old = list(g.objects(cp, EV.chargingPower))
            for o in old:
                g.remove((cp, EV.chargingPower, o))
            g.add((cp, EV.chargingPower, Literal(22.0, datatype=XSD.decimal)))
            conforms, _, _ = validate(data_graph=g, shacl_graph=shapes,
                                      inference=None, advanced=True, debug=False)
            if conforms:
                accepted += 1
            else:
                g.remove((cp, EV.chargingPower, Literal(22.0, datatype=XSD.decimal)))
                for o in old:
                    g.add((cp, EV.chargingPower, o))
        times.append(time.perf_counter() - t0)
        accepted_counts.append(accepted)

    return {
        "points": num_points,
        "graph_triples": num_points * 2 + 1,
        "accepted": accepted_counts[-1],
        "rejected": num_points - accepted_counts[-1],
        "ref_mean_s": round(statistics.mean(times), 3),
        "ref_sd_s": round(statistics.stdev(times), 3) if repeats > 1 else 0.0,
        "per_action_ms": round(1000 * statistics.mean(times) / num_points, 2),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sizes", default="100,200,400,800")
    parser.add_argument("--cross-sizes", default="8,16,32,64")
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    sizes = [int(s) for s in args.sizes.split(",")]
    cross_sizes = [int(s) for s in args.cross_sizes.split(",")]

    result = {"accepted_only": [], "mixed_rejection": [], "cross_target": []}

    print("--- A: accepted-only, zone-local, incremental resolver ---")
    for n in sizes:
        row = run_local_case(n, 0.0, args.repeats)
        result["accepted_only"].append(row)
        print(f"N={n:>4} triples={row['graph_triples']:>5} acc={row['accepted']:>4} "
              f"rej={row['rejected']:>4} t={row['incr_mean_s']:.4f}s "
              f"heap={row['peak_heap_mib']}MiB trace={row['trace_kib']}KiB")

    print("--- B: 50% rejected, zone-local, incremental resolver ---")
    for n in sizes:
        row = run_local_case(n, 0.5, args.repeats)
        result["mixed_rejection"].append(row)
        print(f"N={n:>4} acc={row['accepted']:>4} rej={row['rejected']:>4} "
              f"t={row['incr_mean_s']:.4f}s heap={row['peak_heap_mib']}MiB")

    print("--- C: cross-target feeder budget, reference pySHACL per action ---")
    for n in cross_sizes:
        row = run_cross_target_case(n, args.repeats)
        result["cross_target"].append(row)
        print(f"points={n:>3} acc={row['accepted']:>3} rej={row['rejected']:>3} "
              f"t={row['ref_mean_s']}s ({row['per_action_ms']} ms/action)")

    out = Path(__file__).resolve().parent.parent / "results" / "experiment_scalability_profile.json"
    out.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print("Wrote", out)


if __name__ == "__main__":
    main()
