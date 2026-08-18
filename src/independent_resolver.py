"""
Independent second implementation of the scheduling and resolution stages
(reviewer concern R1-7).

Written directly from the numbered definitions in the manuscript
(Definitions 6-8 and Section III-F), deliberately sharing NO code with the
primary implementation: it does not import resolver, resolver_incremental,
rule_engine, admissibility, state_transition, or trace. It shares only the
RDF parsing library (rdflib), pySHACL as the specification validator, and
the recorded canonical inputs.

Inputs consumed (from a recorded trace):
  * the input graph snapshot (N-Triples lines),
  * the constructed action instances (unordered),
  * the execution configuration (schedule key, role precedence, conflict
    policy, active roles) and shapes path.

Outputs produced independently:
  * the schedule (ascending six-part key, own comparator),
  * per-action gate decisions (role filter, policy guard, conflict gate,
    admissibility via pySHACL),
  * the successor graph and its digest (own canonical N-Triples SHA-256).

experiment_cross_implementation.py compares these against the recorded
values of the primary implementation for every workload trace.
"""
from __future__ import annotations

import hashlib
from typing import Any, Dict, List, Mapping, Tuple

from rdflib import Graph, Literal, URIRef
from rdflib.namespace import XSD

_EX = "http://example.org/building#"


# ---------- own canonicalisation + digest (Section III-F, from the spec) ----------

def canonical_lines(graph: Graph) -> List[str]:
    return sorted(f"{s.n3()} {p.n3()} {o.n3()} ." for (s, p, o) in graph)


def digest(graph: Graph) -> str:
    return hashlib.sha256("\n".join(canonical_lines(graph)).encode("utf-8")).hexdigest()


# ---------- own scheduling (Definition 6, Lemma 1) ----------

def schedule_key(action: Mapping[str, Any], key_fields: List[str]) -> Tuple:
    parts: List[Any] = []
    for field in key_fields:
        value = action.get(field)
        parts.append(value if isinstance(value, (int, float)) else str(value))
    return tuple(parts)


def make_schedule(actions: List[Mapping[str, Any]], key_fields: List[str]) -> List[Mapping[str, Any]]:
    return sorted(actions, key=lambda a: schedule_key(a, key_fields))


# ---------- own graph mutation (single-slot rewrite, Definition 5) ----------

def _typed_literal(value: Any, predicate: str) -> Literal:
    if isinstance(value, bool):
        return Literal(value)
    if predicate.endswith("currentSetpoint"):
        return Literal(float(value), datatype=XSD.decimal)
    if isinstance(value, (int, float)):
        return Literal(value)
    return Literal(str(value))


def apply_single_slot(graph: Graph, action: Mapping[str, Any]) -> None:
    subject = URIRef(str(action["zone"]))
    predicate = URIRef(str(action["predicate"]))
    for obj in list(graph.objects(subject, predicate)):
        graph.remove((subject, predicate, obj))
    graph.add((subject, predicate, _typed_literal(action["value"], str(predicate))))


# ---------- own policy guard (Section IV-C) ----------

def policy_guard(graph: Graph, action: Mapping[str, Any]) -> bool:
    if str(action.get("predicate")) != f"{_EX}currentSetpoint":
        return True
    caps = {
        "occupant": URIRef(f"{_EX}occupantMaxSetpoint"),
        "operator": URIRef(f"{_EX}operatorMaxSetpoint"),
        "emergency": URIRef(f"{_EX}emergencyMaxSetpoint"),
    }
    cap_pred = caps.get(str(action.get("role")))
    if cap_pred is None:
        return True
    policy = URIRef(f"{_EX}Policy")
    proposed = float(action["value"])
    for obj in graph.objects(policy, URIRef(f"{_EX}minSetpoint")):
        if proposed < float(obj.toPython()):
            return False
    for obj in graph.objects(policy, cap_pred):
        if proposed > float(obj.toPython()):
            return False
    return True


# ---------- own admissibility: pySHACL directly (Definition 7) ----------

def admissible(graph: Graph, shapes_graph: Graph) -> bool:
    from pyshacl import validate
    conforms, _, _ = validate(data_graph=graph, shacl_graph=shapes_graph,
                              inference=None, advanced=True, debug=False)
    return bool(conforms)


# ---------- own resolution (Definition 8) ----------

def resolve(
    input_graph: Graph,
    actions: List[Mapping[str, Any]],
    config: Mapping[str, Any],
    shapes_graph: Graph,
) -> Dict[str, Any]:
    key_fields = list(config.get("schedule_key",
                     ["roleRank", "priority", "tsKey", "rid", "bindKey", "aid"]))
    conflict_policy = config.get("conflict_policy", "first_writer_wins")
    active_roles = config.get("active_roles")
    enforce_roles = bool(config.get("enforce_active_roles", False))

    ordered = make_schedule(actions, key_fields)

    working = Graph()
    for triple in input_graph:
        working.add(triple)

    accepted: List[str] = []
    decisions: List[Dict[str, Any]] = []
    written_targets: set[str] = set()

    for action in ordered:
        target = str(action["target_key"])
        # gate (i): role filter
        if enforce_roles and active_roles is not None and str(action.get("role")) not in active_roles:
            decisions.append({"aid": action["aid"], "accepted": False, "reason": "inactive_role"})
            continue
        # gate (ii): policy guard
        if not policy_guard(working, action):
            decisions.append({"aid": action["aid"], "accepted": False, "reason": "policy"})
            continue
        # gate (iii): conflict gate
        if conflict_policy == "first_writer_wins" and target in written_targets:
            decisions.append({"aid": action["aid"], "accepted": False, "reason": "shadowed"})
            continue
        # gate (iv): admissibility (build candidate, validate, keep or revert)
        before = [(s, p, o) for (s, p, o) in working.triples((URIRef(str(action["zone"])),
                                                             URIRef(str(action["predicate"])), None))]
        apply_single_slot(working, action)
        if admissible(working, shapes_graph):
            written_targets.add(target)
            accepted.append(str(action["aid"]))
            decisions.append({"aid": action["aid"], "accepted": True, "reason": "admissible"})
        else:
            subject = URIRef(str(action["zone"]))
            predicate = URIRef(str(action["predicate"]))
            for obj in list(working.objects(subject, predicate)):
                working.remove((subject, predicate, obj))
            for triple in before:
                working.add(triple)
            decisions.append({"aid": action["aid"], "accepted": False, "reason": "inadmissible"})

    return {
        "schedule_aids": [str(a["aid"]) for a in ordered],
        "decisions": decisions,
        "accepted_aids": accepted,
        "successor_digest": digest(working),
    }
