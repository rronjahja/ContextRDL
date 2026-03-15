from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Tuple

from rdflib import Graph, Literal, URIRef

from admissibility import check_admissibility
from state_transition import apply_action
from trace import graph_delta, graph_digest


EX = "http://example.org/building#"
POLICY_NODE = URIRef(f"{EX}Policy")
CURRENT_SETPOINT = f"{EX}currentSetpoint"
MIN_SETPOINT = URIRef(f"{EX}minSetpoint")
ROLE_MAX_PREDICATES = {
    "occupant": URIRef(f"{EX}occupantMaxSetpoint"),
    "operator": URIRef(f"{EX}operatorMaxSetpoint"),
    "emergency": URIRef(f"{EX}emergencyMaxSetpoint"),
}


def _clone_graph(graph: Graph) -> Graph:
    new_graph = Graph()
    for triple in graph:
        new_graph.add(triple)
    return new_graph


def _literal_to_float(value: Literal | Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, Literal):
        return float(value.toPython())
    return float(value)


def _graph_value(graph: Graph, subject: URIRef, predicate: URIRef) -> Optional[Literal]:
    for obj in graph.objects(subject, predicate):
        return obj
    return None


def check_policy_guard(graph: Graph, action: Mapping[str, Any]) -> Tuple[bool, str]:
    if action.get("predicate") != CURRENT_SETPOINT:
        return True, "policy_guard_not_applicable"

    role = str(action.get("role"))
    max_predicate = ROLE_MAX_PREDICATES.get(role)
    if max_predicate is None:
        return True, "policy_guard_not_applicable"

    proposed_value = float(action["value"])

    min_literal = _graph_value(graph, POLICY_NODE, MIN_SETPOINT)
    if min_literal is not None:
        min_value = _literal_to_float(min_literal)
        if min_value is not None and proposed_value < min_value:
            return False, f"policy_min_setpoint_violation:{proposed_value} < {min_value}"

    max_literal = _graph_value(graph, POLICY_NODE, max_predicate)
    if max_literal is not None:
        max_value = _literal_to_float(max_literal)
        if max_value is not None and proposed_value > max_value:
            return False, f"policy_role_cap_violation:{role}:{proposed_value} > {max_value}"

    return True, "policy_guard_passed"


def resolve_actions(
    graph: Graph,
    schedule: List[Mapping[str, Any]],
    shapes_path: str = "shapes/invariants.ttl",
    settings: Optional[Dict[str, Any]] = None,
) -> Tuple[List[Dict[str, Any]], Graph, List[Dict[str, Any]]]:
    settings = settings or {}
    governance_cfg = settings.get("governance", {})
    conflict_policy = governance_cfg.get("conflict_policy", "first_writer_wins")
    schedule_key = settings.get(
        "schedule_key",
        ["roleRank", "priority", "tsKey", "rid", "bindKey", "aid"],
    )

    accepted_actions: List[Dict[str, Any]] = []
    decisions: List[Dict[str, Any]] = []
    current_graph = _clone_graph(graph)
    accepted_targets: Dict[str, Mapping[str, Any]] = {}

    for index, action in enumerate(schedule):
        target_key = str(action["target_key"])
        pre_digest = graph_digest(current_graph)
        decision: Dict[str, Any] = {
            "schedule_index": index,
            "aid": action["aid"],
            "rid": action["rid"],
            "event_id": action.get("event_id"),
            "event_role": action.get("event_role"),
            "role": action.get("role"),
            "target_key": target_key,
            "target": {
                "zone": action["zone"],
                "predicate": action["predicate"],
                "value": action["value"],
            },
            "schedule_key": {field: action.get(field) for field in schedule_key},
            "pre_graph_digest": pre_digest,
        }

        if conflict_policy == "first_writer_wins" and target_key in accepted_targets:
            winner = accepted_targets[target_key]
            decision.update(
                {
                    "accepted": False,
                    "reason": "shadowed_by_prior_accepted_action",
                    "blocked_by_aid": winner["aid"],
                    "blocked_by_rid": winner["rid"],
                    "post_graph_digest": pre_digest,
                    "removed_triples": [],
                    "inserted_triples": [],
                }
            )
            decisions.append(decision)
            continue

        policy_ok, policy_reason = check_policy_guard(current_graph, action)
        if not policy_ok:
            decision.update(
                {
                    "accepted": False,
                    "reason": policy_reason,
                    "policy_reason": policy_reason,
                    "post_graph_digest": pre_digest,
                    "removed_triples": [],
                    "inserted_triples": [],
                }
            )
            decisions.append(decision)
            continue

        candidate_graph = apply_action(current_graph, action)
        candidate_digest = graph_digest(candidate_graph)
        conforms, report = check_admissibility(candidate_graph, shapes_path)

        if conforms:
            removed, inserted = graph_delta(current_graph, candidate_graph)
            current_graph = candidate_graph
            accepted_targets[target_key] = action
            accepted_actions.append(dict(action))
            decision.update(
                {
                    "accepted": True,
                    "reason": "admissible",
                    "policy_reason": policy_reason,
                    "candidate_graph_digest": candidate_digest,
                    "post_graph_digest": candidate_digest,
                    "removed_triples": removed,
                    "inserted_triples": inserted,
                }
            )
        else:
            decision.update(
                {
                    "accepted": False,
                    "reason": "inadmissible",
                    "policy_reason": policy_reason,
                    "validation_report": report,
                    "candidate_graph_digest": candidate_digest,
                    "post_graph_digest": pre_digest,
                    "removed_triples": [],
                    "inserted_triples": [],
                }
            )

        decisions.append(decision)

    return accepted_actions, current_graph, decisions


if __name__ == "__main__":
    graph = Graph()
    graph.parse("shapes/base_graph.ttl", format="turtle")

    schedule = [
        {
            "aid": "demo-aid",
            "rid": "r7",
            "event_id": "demo-evt",
            "event_role": "operator",
            "role": "operator",
            "zone": "http://example.org/building#ZoneB",
            "predicate": "http://example.org/building#currentSetpoint",
            "value": 22,
            "roleRank": 1,
            "priority": 1,
            "tsKey": 0,
            "bindKey": "{}",
            "target_key": "http://example.org/building#ZoneB|http://example.org/building#currentSetpoint",
        }
    ]

    accepted, successor, decisions = resolve_actions(graph, schedule)
    print("Accepted actions:", len(accepted))
    print("Successor digest:", graph_digest(successor))
    print("Decisions:", decisions)