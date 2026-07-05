from __future__ import annotations

import csv
import os
import time
from typing import Dict, List

from rdflib import Graph, Literal, Namespace
from rdflib.namespace import RDF, XSD

from dataset_builder import build_dataset
from resolver import resolve_actions
from rule_engine import evaluate_rules, load_settings, resolve_governance_context, schedule_actions
from rule_loader import load_rules
from trace import graph_digest


EX = Namespace("http://example.org/building#")


def build_synthetic_state(num_zones: int) -> Graph:
    graph = Graph()
    graph.bind("ex", EX)

    for idx in range(num_zones):
        zone = EX[f"ScaleZone{idx:05d}"]
        graph.add((zone, RDF.type, EX.HVAC_Zone))
        graph.add((zone, EX.currentSetpoint, Literal(20.0, datatype=XSD.decimal)))
        graph.add((zone, EX.occupied, Literal(False, datatype=XSD.boolean)))
        graph.add((zone, EX.co2Level, Literal(500.0, datatype=XSD.decimal)))
        graph.add((zone, EX.ventilationMode, Literal("normal")))
        graph.add((zone, EX.emergencyState, Literal(False, datatype=XSD.boolean)))

    graph.add((EX.Policy, RDF.type, EX.ControlPolicy))
    graph.add((EX.Policy, EX.occupantMaxSetpoint, Literal(23.0, datatype=XSD.decimal)))
    graph.add((EX.Policy, EX.operatorMaxSetpoint, Literal(24.0, datatype=XSD.decimal)))
    graph.add((EX.Policy, EX.emergencyMaxSetpoint, Literal(26.0, datatype=XSD.decimal)))
    graph.add((EX.Policy, EX.minSetpoint, Literal(18.0, datatype=XSD.decimal)))
    return graph


def build_synthetic_events(num_actions: int) -> List[Dict[str, object]]:
    timestamp = "2026-03-14T09:00:00Z"
    events: List[Dict[str, object]] = []
    for idx in range(num_actions):
        zone_uri = str(EX[f"ScaleZone{idx:05d}"])
        events.append(
            {
                "eid": f"scale-preheat-{idx:05d}",
                "timestamp": timestamp,
                "type": "PreheatRequest",
                "role": "operator",
                "payload": {
                    "zone": zone_uri,
                    "target": 22,
                },
            }
        )
    return events


def run_scalability_case(num_actions: int):
    settings = load_settings("configs/settings.json")
    context = resolve_governance_context(settings=settings, contexts_path="data/contexts.json")
    rules = load_rules("configs/rules.json")

    graph_t = build_synthetic_state(num_actions)
    events = build_synthetic_events(num_actions)
    dataset, window_meta = build_dataset(graph_t, events, settings=settings)
    enabled = evaluate_rules(dataset, rules, settings=settings, context=context, window_meta=window_meta)
    schedule = schedule_actions(enabled, settings=settings)

    start = time.perf_counter()
    accepted, graph_next, decisions = resolve_actions(
        graph_t,
        schedule,
        shapes_path="shapes/invariants.ttl",
        settings=settings,
    )
    elapsed = time.perf_counter() - start

    result = {
        "actions": len(enabled),
        "accepted": len(accepted),
        "rejected": len(enabled) - len(accepted),
        "time_seconds": round(elapsed, 6),
        "successor_digest": graph_digest(graph_next),
        "rejection_reasons": sorted(
            {decision.get("reason", "unknown") for decision in decisions if not decision.get("accepted")}
        ),
    }
    return result


def run_scalability_experiment(sizes=None, csv_path: str = "results/scalability.csv"):
    sizes = sizes or [10, 50, 100, 200, 400, 800]
    os.makedirs(os.path.dirname(csv_path) or ".", exist_ok=True)

    rows = []
    for size in sizes:
        result = run_scalability_case(size)
        rows.append(result)
        print(
            "Actions:", result["actions"],
            "Accepted:", result["accepted"],
            "Rejected:", result["rejected"],
            "Time:", result["time_seconds"], "seconds",
        )

    with open(csv_path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["actions", "accepted", "rejected", "time_seconds", "successor_digest", "rejection_reasons"],
        )
        writer.writeheader()
        writer.writerows(rows)

    return rows


if __name__ == "__main__":
    run_scalability_experiment()