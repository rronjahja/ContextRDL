"""
Independent second implementation of the scheduling and resolution stages
(reviewer concern R1-7).

Written directly from the numbered definitions in the manuscript
(Definitions 6, 8 and 9, and Section III-F), deliberately sharing NO code with the
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
import re
from decimal import Context, Decimal, InvalidOperation, MAX_EMAX, MIN_EMIN, localcontext
from typing import Any, Dict, List, Mapping, Tuple

from rdflib import Graph, Literal, URIRef
from rdflib.namespace import XSD

_EX = "http://example.org/building#"


def validate_policy_input(graph: Graph, namespace: str = _EX,
                          names: Tuple[str, ...] = ("minSetpoint", "occupantMaxSetpoint",
                                                   "operatorMaxSetpoint", "emergencyMaxSetpoint")) -> None:
    """Independent policy-profile check; domain-state violations are separate."""
    node = URIRef(namespace + "Policy")
    for name in names:
        predicate = URIRef(namespace + name)
        values = tuple(graph.objects(node, predicate))
        if len(values) != 1:
            raise ValueError(f"{predicate}: expected exactly one policy value, found {len(values)}")
        value = values[0]
        valid = (isinstance(value, Literal) and value.datatype == XSD.decimal
                 and value.language is None and not value.ill_typed
                 and isinstance(value.toPython(), Decimal)
                 and re.fullmatch(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)", str(value)) is not None)
        try:
            valid = valid and Decimal(str(value)).is_finite()
        except InvalidOperation:
            valid = False
        if not valid:
            raise ValueError(f"{predicate}: expected a finite, well-formed xsd:decimal")


# ---------- own canonicalisation + digest (Section III-F, from the spec) ----------

def canonical_lines(graph: Graph) -> List[str]:
    return sorted(line for line in graph.serialize(format="nt").split("\n") if line)


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

def _numeric_decimal(value: Any) -> Decimal:
    if isinstance(value, Literal):
        if value.ill_typed or value.language is not None:
            raise ValueError("Invalid numeric command")
        value = str(value)
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise ValueError("Invalid numeric command")
    text = str(value)
    if re.fullmatch(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?", text) is None:
        raise ValueError("Invalid numeric command")
    number = Decimal(text)
    if not number.is_finite():
        raise ValueError("Invalid numeric command")
    return number


def _typed_literal(value: Any, predicate: str) -> Literal:
    if predicate.endswith(("currentSetpoint", "chargingPower", "co2Level")):
        number = _numeric_decimal(value)
        text = format(number, "f") if number else "0.0"
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        if "." not in text:
            text += ".0"
        return Literal(text, datatype=XSD.decimal, normalize=False)
    if isinstance(value, bool):
        return Literal(value)
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

def policy_guard(graph: Graph, action: Mapping[str, Any], domain: str = "hvac") -> Tuple[bool, str]:
    """Returns (passed, reason class). The reason classes are the vocabulary
    fixed by the execution configuration: a reason code is the text before
    the first colon; implementations may append details after a colon."""
    namespace = "http://example.org/ev#" if domain == "ev" else _EX
    property_name = "chargingPower" if domain == "ev" else "currentSetpoint"
    minimum_name = "minPower" if domain == "ev" else "minSetpoint"
    if str(action.get("predicate")) != namespace + property_name:
        return True, "policy_guard_not_applicable"
    caps = {
        "occupant": URIRef(f"{_EX}occupantMaxSetpoint"),
        "operator": URIRef(f"{_EX}operatorMaxSetpoint"),
        "emergency": URIRef(f"{_EX}emergencyMaxSetpoint"),
    }
    if domain == "ev":
        caps = {role: URIRef(namespace + role + "MaxPower")
                for role in ("safety", "grid", "fleet", "driver")}
    cap_pred = caps.get(str(action.get("role")))
    if cap_pred is None:
        return True, "policy_guard_not_applicable"
    policy = URIRef(namespace + "Policy")
    proposed = _numeric_decimal(action["value"])
    for obj in graph.objects(policy, URIRef(namespace + minimum_name)):
        if proposed < _numeric_decimal(obj):
            return False, "policy_min_violation"
    for obj in graph.objects(policy, cap_pred):
        if proposed > _numeric_decimal(obj):
            return False, "policy_role_cap_violation"
    return True, "policy_guard_passed"


# ---------- own admissibility: pySHACL directly (Definition 8) ----------

def admissible(graph: Graph, shapes_graph: Graph) -> bool:
    from pyshacl import validate
    # Independent precision bound for the finite decimal SUM used by EV.
    whole, fraction, count = 1, 0, 0
    for term in graph.objects():
        if not isinstance(term, Literal) or term.datatype not in (XSD.decimal, XSD.integer):
            continue
        try:
            number = _numeric_decimal(term)
        except ValueError:
            continue
        whole = max(whole, number.adjusted() + 1)
        fraction = max(fraction, -number.as_tuple().exponent)
        count += 1
    context = Context(prec=whole + fraction + len(str(count + 1)) + 1,
                      Emax=MAX_EMAX, Emin=MIN_EMIN)
    try:
        with localcontext(context):
            conforms, _, _ = validate(data_graph=graph, shacl_graph=shapes_graph,
                                      inference=None, advanced=True, debug=False)
    except InvalidOperation:
        return False
    return bool(conforms)

# ---------- own resolution (Definition 9; reason-code classes of Definition 11 (x)) ----------

def resolve(
    input_graph: Graph,
    actions: List[Mapping[str, Any]],
    config: Mapping[str, Any],
    shapes_graph: Graph,
) -> Dict[str, Any]:
    key_fields = list(config.get("schedule_key",
                     ["roleRank", "priority", "tsKey", "rid", "bindKey", "aid"]))
    conflict_policy = config.get("conflict_policy", "first_writer_wins")
    if conflict_policy != "first_writer_wins":
        raise ValueError(f"Unsupported conflict policy: {conflict_policy}")
    domain = config.get("domain", "hvac")
    if domain not in {"hvac", "ev"}:
        raise ValueError(f"Unsupported policy domain: {domain}")
    if domain == "ev":
        validate_policy_input(input_graph, "http://example.org/ev#",
                              ("minPower", "safetyMaxPower", "gridMaxPower", "fleetMaxPower", "driverMaxPower"))
        budget_terms = list(input_graph.objects(URIRef("http://example.org/ev#Feeder1"),
                                               URIRef("http://example.org/ev#feederBudget")))
        if len(budget_terms) != 1:
            raise ValueError("feederBudget: expected exactly one value")
        budget = budget_terms[0]
        if (not isinstance(budget, Literal) or budget.datatype != XSD.decimal
                or budget.ill_typed or not isinstance(budget.toPython(), Decimal)
                or re.fullmatch(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)", str(budget)) is None):
            raise ValueError("feederBudget: expected a finite xsd:decimal")
        if not _numeric_decimal(budget).is_finite() or _numeric_decimal(budget) < 0:
            raise ValueError("feederBudget: expected a nonnegative decimal")
    else:
        validate_policy_input(input_graph)
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
            decisions.append({"aid": action["aid"], "accepted": False, "reason": "inactive_role",
                              "post_graph_digest": digest(working)})
            continue
        # gate (ii): policy guard
        passed, reason = policy_guard(working, action, domain)
        if not passed:
            decisions.append({"aid": action["aid"], "accepted": False, "reason": reason,
                              "post_graph_digest": digest(working)})
            continue
        # gate (iii): conflict gate
        if conflict_policy == "first_writer_wins" and target in written_targets:
            decisions.append({"aid": action["aid"], "accepted": False,
                              "reason": "shadowed_by_prior_accepted_action",
                              "post_graph_digest": digest(working)})
            continue
        # gate (iv): admissibility (build candidate, validate, keep or revert)
        before = [(s, p, o) for (s, p, o) in working.triples((URIRef(str(action["zone"])),
                                                             URIRef(str(action["predicate"])), None))]
        apply_single_slot(working, action)
        if admissible(working, shapes_graph):
            written_targets.add(target)
            accepted.append(str(action["aid"]))
            decisions.append({"aid": action["aid"], "accepted": True, "reason": "admissible",
                              "post_graph_digest": digest(working)})
        else:
            subject = URIRef(str(action["zone"]))
            predicate = URIRef(str(action["predicate"]))
            for obj in list(working.objects(subject, predicate)):
                working.remove((subject, predicate, obj))
            for triple in before:
                working.add(triple)
            decisions.append({"aid": action["aid"], "accepted": False, "reason": "inadmissible",
                              "post_graph_digest": digest(working)})

    return {
        "schedule_aids": [str(a["aid"]) for a in ordered],
        "decisions": decisions,
        "accepted_aids": accepted,
        "successor_digest": digest(working),
    }
