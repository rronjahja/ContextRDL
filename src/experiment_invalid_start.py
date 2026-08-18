"""
Experiment: behaviour when the input graph G_t is NOT admissible.

Addresses reviewer concern R1-1: the admissibility guarantee is an
*invariant-preservation* guarantee (admissible G_t -> admissible G_{t+1}),
not an unconditional one. This experiment characterises what the evaluated
implementation does when the precondition is violated:

  Scenario "valid"          : base graph (control; precondition holds).
  Scenario "single_violation": ZoneA currentSetpoint = 25 (> comfort cap 23).
  Scenario "double_violation": ZoneA setpoint = 25 AND ZoneC in emergency
                               state with non-emergency ventilation.

For each scenario we run the default workload through the reference resolver
under full-graph SHACL validation (the specification validator) and report:
  * per-action decisions,
  * whether any action repaired the pre-existing violation(s),
  * admissibility of the final state,
  * whether the run "recovered" (final state admissible although G_t was not).

Expected behaviour (documented in the manuscript):
  * single_violation: every action whose candidate graph still contains the
    pre-existing violation is rejected; the single scheduled action that
    fully repairs the graph (the operator cap r2 on ZoneA) is accepted, after
    which resolution proceeds normally -> the step recovers.
  * double_violation: no single scheduled action repairs BOTH violations, so
    every action is rejected even though the pair {r2, r6} would jointly
    repair the graph -> the step does not recover; a separate repair mode
    would be required.

Usage (from repo root or src/):
    python experiment_invalid_start.py
Writes results/experiment_invalid_start.json
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from rdflib import Literal, URIRef
from rdflib.namespace import XSD

from admissibility import check_admissibility_shacl
from dataset_builder import build_dataset, load_events, load_state
from resolver import resolve_actions
from rule_engine import evaluate_rules, load_settings, resolve_governance_context, schedule_actions
from rule_loader import load_rules
from trace import graph_digest

EX = "http://example.org/building#"
ZONE_A = URIRef(f"{EX}ZoneA")
ZONE_C = URIRef(f"{EX}ZoneC")
CURRENT_SETPOINT = URIRef(f"{EX}currentSetpoint")
VENTILATION_MODE = URIRef(f"{EX}ventilationMode")
EMERGENCY_STATE = URIRef(f"{EX}emergencyState")

SHAPES = "shapes/invariants.ttl"


def _set_single_value(graph, subject, predicate, literal):
    for obj in list(graph.objects(subject, predicate)):
        graph.remove((subject, predicate, obj))
    graph.add((subject, predicate, literal))


def make_start_graph(scenario: str):
    graph = load_state("shapes/base_graph.ttl")
    if scenario == "valid":
        return graph
    if scenario in ("single_violation", "double_violation"):
        # ZoneA setpoint 25 violates ZoneAComfortCapShape (<= 23)
        _set_single_value(graph, ZONE_A, CURRENT_SETPOINT, Literal(25.0, datatype=XSD.decimal))
    if scenario == "double_violation":
        # ZoneC: emergencyState true with non-emergency ventilation violates
        # EmergencyVentLockShape
        _set_single_value(graph, ZONE_C, EMERGENCY_STATE, Literal(True))
        _set_single_value(graph, ZONE_C, VENTILATION_MODE, Literal("high"))
    return graph


def run_scenario(scenario: str):
    settings = load_settings("configs/settings.json")
    context = resolve_governance_context(settings=settings, contexts_path="data/contexts.json")
    graph_t = make_start_graph(scenario)
    events = load_events("data/events.jsonl")
    rules = load_rules("configs/rules.json")

    start_conforms, start_report = check_admissibility_shacl(graph_t, SHAPES)

    dataset, window_meta = build_dataset(graph_t, events, settings=settings)
    window_meta = dict(window_meta)
    window_meta["governance_context"] = context
    enabled = evaluate_rules(dataset, rules, settings=settings, context=context, window_meta=window_meta)
    schedule = schedule_actions(enabled, settings=settings)

    accepted, successor, decisions = resolve_actions(
        graph_t, schedule, shapes_path=SHAPES, settings=settings
    )
    final_conforms, final_report = check_admissibility_shacl(successor, SHAPES)

    decision_rows = [
        {
            "order": d["schedule_index"] + 1,
            "rid": d["rid"],
            "target": d["target_key"].split("#")[-1],
            "value": d["target"]["value"],
            "accepted": d["accepted"],
            "reason": d["reason"],
        }
        for d in decisions
    ]

    return {
        "scenario": scenario,
        "start_admissible": start_conforms,
        "start_violations": None if start_conforms else start_report.strip().splitlines()[2:],
        "enabled": len(enabled),
        "accepted": len(accepted),
        "accepted_rids": [a["rid"] for a in accepted],
        "final_admissible": final_conforms,
        "recovered": (not start_conforms) and final_conforms,
        "successor_digest": graph_digest(successor),
        "decisions": decision_rows,
    }


def main():
    # Force the specification validator: the zone-scoped incremental gate
    # assumes the precondition and is not meaningful on invalid starts.
    os.environ["ADMISSIBILITY_REGIME"] = "shacl"

    results = {}
    for scenario in ("valid", "single_violation", "double_violation"):
        summary = run_scenario(scenario)
        results[scenario] = summary
        print(f"\n=== scenario: {scenario} ===")
        print("start admissible :", summary["start_admissible"])
        print("accepted actions :", summary["accepted"], summary["accepted_rids"])
        print("final admissible :", summary["final_admissible"])
        print("recovered        :", summary["recovered"])
        for row in summary["decisions"]:
            mark = "ACCEPT" if row["accepted"] else "reject"
            print(f"  {row['order']}. {row['rid']:<3} {row['target']:<16} := {row['value']!s:<10} {mark}  ({row['reason']})")

    out = Path(__file__).resolve().parent.parent / "results" / "experiment_invalid_start.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print("\nWrote", out)
    return results


if __name__ == "__main__":
    main()
