"""
Cross-implementation reproducibility (reviewer concern R1-7).

Runs the clean-room second implementation (independent_resolver.py) on the
canonical inputs recorded in each workload trace and compares, field by
field, against the primary implementation's recorded outputs:

  * schedule (order of action identifiers),
  * per-action accept/reject decisions and reason class,
  * accepted set,
  * successor-graph digest.

Traces are (re)generated freshly by the primary implementation for every
workload family, then handed to the second implementation.

Usage:
    python experiment_cross_implementation.py
Writes results/experiment_cross_implementation.json
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from rdflib import Graph

import independent_resolver as second
from engine import run_engine

# --- workload builders -------------------------------------------------------
# The default workload uses the shipped configs. Additional workloads reuse
# the same run_engine entry point with targeted event lists (mirroring the
# experiment scripts already in the repository).

TIE_EVENTS = [
    {"eid": "tie-1", "type": "PreheatRequest", "timestamp": "2026-03-14T09:00:00Z",
     "role": "operator", "payload": {"zone": "http://example.org/building#ZoneB", "target": 21}},
    {"eid": "tie-2", "type": "PreheatRequest", "timestamp": "2026-03-14T09:00:00Z",
     "role": "operator", "payload": {"zone": "http://example.org/building#ZoneB", "target": 22}},
    {"eid": "tie-3", "type": "CO2Spike", "timestamp": "2026-03-14T09:00:00Z",
     "role": "operator", "payload": {"zone": "http://example.org/building#ZoneC", "mode": "high"}},
]

GOV_EVENTS = [
    {"eid": "gov-1", "type": "DemandResponsePeak", "timestamp": "2026-03-14T09:00:00Z",
     "role": "operator", "payload": {"zone": "http://example.org/building#ZoneA", "cap": 21}},
    {"eid": "gov-2", "type": "OccupantSetpointReq", "timestamp": "2026-03-14T09:00:00Z",
     "role": "occupant", "payload": {"zone": "http://example.org/building#ZoneA", "delta": 1}},
    {"eid": "gov-3", "type": "OccupancyDetected", "timestamp": "2026-03-14T09:00:00Z",
     "role": "occupant", "payload": {"zone": "http://example.org/building#ZoneB"}},
]

WORKLOADS: Dict[str, Dict[str, Any]] = {
    "default": {},
    "tie_conflict": {"events": TIE_EVENTS},
    "governance_conflict": {"events": GOV_EVENTS},
}


def reason_class(reason: str) -> str:
    if reason.startswith("shadowed"):
        return "shadowed"
    if reason.startswith("policy"):
        return "policy"
    if reason in ("admissible",):
        return "admissible"
    if reason in ("inadmissible",):
        return "inadmissible"
    if reason == "inactive_role":
        return "inactive_role"
    return reason


def compare(trace: Dict[str, Any], shapes_graph: Graph) -> Dict[str, Any]:
    input_graph = Graph()
    input_graph.parse(data="\n".join(trace["input_graph"]["triples"]), format="nt")

    settings = trace["settings"]
    context = trace["window"].get("governance_context", {})
    config = {
        "schedule_key": settings.get("schedule_key"),
        "conflict_policy": settings.get("governance", {}).get("conflict_policy", "first_writer_wins"),
        "active_roles": context.get("active_roles"),
        "enforce_active_roles": context.get("enforce_active_roles", False),
    }

    # canonical unordered inputs: the constructed action instances
    actions = list(trace["schedule"])

    result = second.resolve(input_graph, actions, config, shapes_graph)

    recorded_schedule = [d["aid"] for d in sorted(trace["decisions"], key=lambda d: d["schedule_index"])]
    recorded_decisions = [
        (d["aid"], bool(d["accepted"]), reason_class(str(d["reason"])))
        for d in sorted(trace["decisions"], key=lambda d: d["schedule_index"])
    ]
    second_decisions = [
        (d["aid"], bool(d["accepted"]), reason_class(str(d["reason"])))
        for d in result["decisions"]
    ]
    recorded_accepted = [a["aid"] for a in trace["accepted_actions"]]

    return {
        "schedule_match": recorded_schedule == result["schedule_aids"],
        "decisions_match": recorded_decisions == second_decisions,
        "accepted_match": recorded_accepted == result["accepted_aids"],
        "digest_match": trace["successor_graph"]["digest"] == result["successor_digest"],
        "recorded_digest": trace["successor_graph"]["digest"],
        "independent_digest": result["successor_digest"],
        "n_actions": len(actions),
    }


def main():
    shapes_graph = Graph()
    shapes_path = Path(__file__).resolve().parent.parent / "shapes" / "invariants.ttl"
    shapes_graph.parse(data=shapes_path.read_text(encoding="utf-8"), format="turtle")

    outcomes: Dict[str, Any] = {}
    for name, kwargs in WORKLOADS.items():
        *_, trace = run_engine(save_trace_file=False, **kwargs)
        outcomes[name] = compare(trace, shapes_graph)
        o = outcomes[name]
        print(f"{name:<22} n={o['n_actions']}  schedule={o['schedule_match']}  "
              f"decisions={o['decisions_match']}  accepted={o['accepted_match']}  digest={o['digest_match']}")

    out = Path(__file__).resolve().parent.parent / "results" / "experiment_cross_implementation.json"
    out.write_text(json.dumps(outcomes, indent=2), encoding="utf-8")
    print("Wrote", out)
    return outcomes


if __name__ == "__main__":
    main()
