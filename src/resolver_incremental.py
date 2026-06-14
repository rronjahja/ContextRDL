"""
Incremental drop-in replacement for ``resolver.resolve_actions``.

Same signature and same return-tuple order as the original:
    resolve_actions_incremental(graph, schedule, shapes_path=..., settings=...)
    -> (accepted_actions, successor_graph, decisions)

Same semantics (same gates, same first-writer-wins, same reason strings).
The single change is *how* admissibility is computed:

  * Original: clone graph, apply action to clone, compute full digest before+after,
    run full-graph admissibility check, diff both full graphs. O(|graph|) per action.
  * Incremental: one mutable working graph; apply in place; check admissibility only
    over the touched zone; undo on rejection. O(1)-per-action w.r.t. graph size.

Equivalence against the original is asserted by ``test_resolver_equivalence.py``.

The ``record_digests`` kwarg (default False) gates the pre/post/candidate digest
and inserted/removed triple fields -- these are only needed when writing a trace.
"""
from __future__ import annotations

import hashlib
from typing import Any, Dict, List, Mapping, Optional, Tuple

from rdflib import Graph, URIRef

from admissibility import check_admissibility_incremental
from state_transition import make_literal


# ---------------------------------------------------------------------------
# Constants (mirror the originals in resolver.py)
# ---------------------------------------------------------------------------

_EX = "http://example.org/building#"
_POLICY_NODE = URIRef(f"{_EX}Policy")
_CURRENT_SETPOINT = f"{_EX}currentSetpoint"
_MIN_SETPOINT = URIRef(f"{_EX}minSetpoint")
_ROLE_MAX_PREDICATES = {
    "occupant":  URIRef(f"{_EX}occupantMaxSetpoint"),
    "operator":  URIRef(f"{_EX}operatorMaxSetpoint"),
    "emergency": URIRef(f"{_EX}emergencyMaxSetpoint"),
}


# ---------------------------------------------------------------------------
# Policy guard (reason strings and behaviour identical to resolver.py)
# ---------------------------------------------------------------------------

def _graph_value(graph: Graph, subject: URIRef, predicate: URIRef):
    for obj in graph.objects(subject, predicate):
        return obj
    return None


def _as_float(value) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value.toPython() if hasattr(value, "toPython") else value)
    except Exception:
        return None


def check_policy_guard(graph: Graph, action: Mapping[str, Any]) -> Tuple[bool, str]:
    if action.get("predicate") != _CURRENT_SETPOINT:
        return True, "policy_guard_not_applicable"

    role = str(action.get("role"))
    max_pred = _ROLE_MAX_PREDICATES.get(role)
    if max_pred is None:
        return True, "policy_guard_not_applicable"

    try:
        proposed = float(action["value"])
    except (TypeError, ValueError):
        return True, "policy_guard_not_applicable"

    min_val = _as_float(_graph_value(graph, _POLICY_NODE, _MIN_SETPOINT))
    if min_val is not None and proposed < min_val:
        return False, f"policy_min_setpoint_violation:{proposed} < {min_val}"

    max_val = _as_float(_graph_value(graph, _POLICY_NODE, max_pred))
    if max_val is not None and proposed > max_val:
        return False, f"policy_role_cap_violation:{role}:{proposed} > {max_val}"

    return True, "policy_guard_passed"


# ---------------------------------------------------------------------------
# Digest helpers (only used when record_digests=True)
# ---------------------------------------------------------------------------

def _graph_digest(graph: Graph) -> str:
    lines = sorted(f"{s.n3()} {p.n3()} {o.n3()} ." for s, p, o in graph)
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def _triple_str(triple) -> str:
    s, p, o = triple
    return f"{s.n3()} {p.n3()} {o.n3()} ."


# ---------------------------------------------------------------------------
# The incremental resolver
# ---------------------------------------------------------------------------

DEFAULT_SCHEDULE_KEY = ["roleRank", "priority", "tsKey", "rid", "bindKey", "aid"]


def resolve_actions_incremental(
    graph: Graph,
    schedule: List[Mapping[str, Any]],
    shapes_path: str = "shapes/invariants.ttl",
    settings: Optional[Dict[str, Any]] = None,
    record_digests: bool = False,
) -> Tuple[List[Dict[str, Any]], Graph, List[Dict[str, Any]]]:
    """Same contract as ``resolver.resolve_actions`` but O(1)-per-action w.r.t. graph size."""

    settings = settings or {}
    governance_cfg = settings.get("governance", {})
    conflict_policy = governance_cfg.get("conflict_policy", "first_writer_wins")
    schedule_key_fields = settings.get("schedule_key", DEFAULT_SCHEDULE_KEY)

    # One mutable working graph -- we mutate in place and undo on rejection.
    current_graph = Graph()
    for t in graph:
        current_graph.add(t)

    accepted_actions: List[Dict[str, Any]] = []
    accepted_targets: Dict[str, Mapping[str, Any]] = {}
    decisions: List[Dict[str, Any]] = []

    for index, action in enumerate(schedule):
        target_key = str(action["target_key"])
        zone = URIRef(action["zone"])
        predicate = URIRef(action["predicate"])
        new_literal = make_literal(action["predicate"], action["value"])

        pre_digest = _graph_digest(current_graph) if record_digests else None

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
            "schedule_key": {field: action.get(field) for field in schedule_key_fields},
        }
        if record_digests:
            decision["pre_graph_digest"] = pre_digest

        # --- Gate 1: first-writer-wins shadowing ---
        if conflict_policy == "first_writer_wins" and target_key in accepted_targets:
            winner = accepted_targets[target_key]
            decision.update({
                "accepted": False,
                "reason": "shadowed_by_prior_accepted_action",
                "blocked_by_aid": winner["aid"],
                "blocked_by_rid": winner["rid"],
            })
            if record_digests:
                decision.update({
                    "post_graph_digest": pre_digest,
                    "removed_triples": [],
                    "inserted_triples": [],
                })
            decisions.append(decision)
            continue

        # --- Gate 2: policy guard ---
        pg_ok, pg_reason = check_policy_guard(current_graph, action)
        if not pg_ok:
            decision.update({
                "accepted": False,
                "reason": pg_reason,
                "policy_reason": pg_reason,
            })
            if record_digests:
                decision.update({
                    "post_graph_digest": pre_digest,
                    "removed_triples": [],
                    "inserted_triples": [],
                })
            decisions.append(decision)
            continue

        # --- Gate 3: admissibility (in place, undo on reject) ---
        old_triples = list(current_graph.triples((zone, predicate, None)))
        new_triple = (zone, predicate, new_literal)
        for t in old_triples:
            current_graph.remove(t)
        current_graph.add(new_triple)

        conforms, report = check_admissibility_incremental(current_graph, focus_zones={zone})
        candidate_digest = _graph_digest(current_graph) if record_digests else None

        if conforms:
            accepted_targets[target_key] = action
            accepted_actions.append(dict(action))
            decision.update({
                "accepted": True,
                "reason": "admissible",
                "policy_reason": pg_reason,
            })
            if record_digests:
                decision.update({
                    "candidate_graph_digest": candidate_digest,
                    "post_graph_digest": candidate_digest,
                    "removed_triples": sorted(_triple_str(t) for t in old_triples),
                    "inserted_triples": [_triple_str(new_triple)],
                })
        else:
            # Undo mutation
            current_graph.remove(new_triple)
            for t in old_triples:
                current_graph.add(t)
            decision.update({
                "accepted": False,
                "reason": "inadmissible",
                "policy_reason": pg_reason,
                "validation_report": report,
            })
            if record_digests:
                decision.update({
                    "candidate_graph_digest": candidate_digest,
                    "post_graph_digest": pre_digest,
                    "removed_triples": [],
                    "inserted_triples": [],
                })

        decisions.append(decision)

    return accepted_actions, current_graph, decisions
