"""
Governance experiment v2.

The existing governance experiment flips operator/occupant precedence and
shows the committed setpoint for ZoneB changes from 21 to 22.  What actually
happens in the reversed case is that the operator's demand-response *cap*
(a safety-relevant constraint) gets silently shadowed because the occupant
wrote first.  That is not a governance demonstration -- it is a design
consequence of first-writer-wins that a careful reader will read as a bug.

This experiment constructs a cleaner scenario that exercises both gates:

    ZoneA is subject to the SHACL ZoneAComfortCapShape: setpoint <= 23.
    Policy says operatorMaxSetpoint = 24, so an operator request of 24
    passes the policy guard but fails admissibility.

Three actions on ZoneA currentSetpoint:
    A_unsafe:  operator, priority 0, value 24   (policy OK, admissibility FAIL)
    A_op:      operator, priority 1, value 23   (admissible; hits ZoneA cap exactly)
    A_occ:     occupant, priority 2, value 22   (admissible; below cap)

Case 1 -- default precedence (emergency > operator > occupant):
    schedule = [A_unsafe, A_op, A_occ]
    A_unsafe: policy OK, admissibility REJECTS (24 > ZoneA cap 23)
    A_op:     target not claimed -> ACCEPTED at 23
    A_occ:    target claimed by A_op -> SHADOWED
    final setpoint = 23
    rejection carries reason "inadmissible" with SHACL report attached.

Case 2 -- reversed precedence (occupant > operator):
    schedule = [A_occ, A_unsafe, A_op]
    A_occ:    ACCEPTED at 22
    A_unsafe: target claimed -> SHADOWED (does not even reach admissibility)
    A_op:     target claimed -> SHADOWED
    final setpoint = 22

This shows three things the original experiment does not:
  1. Admissibility actually fires (it never does in the default workload trace).
  2. Governance changes the committed value but *within* the admissible set.
  3. The system is safe under both governance configurations, for different
     reasons (admissibility in Case 1, shadowing in Case 2).
"""
from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Dict, List

from rdflib import Graph, URIRef

from dataset_builder import load_state
from resolver import resolve_actions as resolve_original
from rule_engine import schedule_actions


ZONE_A = "http://example.org/building#ZoneA"
CURRENT_SETPOINT = "http://example.org/building#currentSetpoint"
TARGET_KEY = f"{ZONE_A}|{CURRENT_SETPOINT}"


def _graph_digest(graph: Graph) -> str:
    lines = sorted(f"{s.n3()} {p.n3()} {o.n3()} ." for s, p, o in graph)
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def _make_action(rid: str, role: str, role_rank: int, priority: int, value: float,
                 bind_i: int) -> Dict[str, Any]:
    bind_key = json.dumps({"i": bind_i}, sort_keys=True, separators=(",", ":"))
    aid_payload = {
        "rid": rid, "eid": f"gov-{rid}", "window_id": "urn:window",
        "predicate": CURRENT_SETPOINT, "bindings": {"i": bind_i},
    }
    aid = hashlib.sha256(
        json.dumps(aid_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {
        "aid": aid,
        "rid": rid,
        "event_uri": f"urn:event:gov-{rid}",
        "event_id":  f"gov-{rid}",
        "event_ts":  "2026-03-14T09:00:00Z",
        "event_role": role,
        "window_id": "urn:window",
        "zone": ZONE_A,
        "predicate": CURRENT_SETPOINT,
        "target_key": TARGET_KEY,
        "value": value,
        "priority": priority,
        "role": role,
        "roleRank": role_rank,
        "tsKey": 0,
        "bindKey": bind_key,
        "bindings": {"i": bind_i},
    }


def _build_actions(role_rank_map: Dict[str, int]) -> List[Dict[str, Any]]:
    return [
        _make_action("A_unsafe", "operator", role_rank_map["operator"], 0, 24.0, 1),
        _make_action("A_op",     "operator", role_rank_map["operator"], 1, 23.0, 2),
        _make_action("A_occ",    "occupant", role_rank_map["occupant"], 2, 22.0, 3),
    ]


def run_case(label: str, role_rank_map: Dict[str, int]) -> Dict[str, Any]:
    state = load_state("shapes/base_graph.ttl")
    settings = {
        "governance": {"conflict_policy": "first_writer_wins"},
        "schedule_key": ["roleRank", "priority", "tsKey", "rid", "bindKey", "aid"],
    }
    actions = _build_actions(role_rank_map)
    schedule = schedule_actions(actions, settings=settings)
    accepted, successor, decisions = resolve_original(
        state, schedule, shapes_path="shapes/invariants.ttl", settings=settings
    )

    # Extract the ZoneA setpoint from the successor
    final_sp = None
    from rdflib import URIRef as U
    for obj in successor.objects(U(ZONE_A), U(CURRENT_SETPOINT)):
        final_sp = obj.toPython() if hasattr(obj, "toPython") else str(obj)

    decision_summary = [
        {
            "rid":      d["rid"],
            "accepted": d["accepted"],
            "reason":   d["reason"],
            "value":    d.get("target", {}).get("value"),
        }
        for d in decisions
    ]

    return {
        "case":            label,
        "role_rank_map":   role_rank_map,
        "schedule_order":  [a["rid"] for a in schedule],
        "accepted_rids":   [a["rid"] for a in accepted],
        "final_ZoneA_setpoint": final_sp,
        "successor_digest":     _graph_digest(successor),
        "decisions":       decision_summary,
    }


def main():
    default = {"emergency": 0, "operator": 1, "occupant": 2}
    reversed_ = {"emergency": 0, "operator": 2, "occupant": 1}

    case_1 = run_case("default (operator > occupant)",  default)
    case_2 = run_case("reversed (occupant > operator)", reversed_)

    out = {"cases": [case_1, case_2]}
    os.makedirs("results", exist_ok=True)
    with open("results/experiment_governance_v2.json", "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2)

    for c in (case_1, case_2):
        print(f"\n=== {c['case']} ===")
        print(f"schedule order:  {c['schedule_order']}")
        print(f"accepted:        {c['accepted_rids']}")
        print(f"ZoneA setpoint:  {c['final_ZoneA_setpoint']}")
        print(f"digest:          {c['successor_digest'][:16]}")
        for d in c["decisions"]:
            print(f"  {d['rid']}: accepted={d['accepted']:<5}  value={d['value']:>5}  reason={d['reason']}")

    print("\nWrote results/experiment_governance_v2.json")


if __name__ == "__main__":
    main()
