"""Well-formed policy inputs, checked independently of domain-state validity.

The reference and incremental production resolvers share this input profile.
The second implementation deliberately implements its own check.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Iterable

from rdflib import Graph, Literal, URIRef
from rdflib.namespace import XSD

HVAC_NAMESPACE = "http://example.org/building#"
HVAC_POLICY_NODE = URIRef(HVAC_NAMESPACE + "Policy")
HVAC_POLICY_PREDICATES = tuple(URIRef(HVAC_NAMESPACE + name) for name in (
    "minSetpoint", "occupantMaxSetpoint", "operatorMaxSetpoint", "emergencyMaxSetpoint"))
DECIMAL_LEXICAL = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)")


class PolicyProfileError(ValueError):
    """The policy configuration has no unambiguous finite numeric meaning."""


def policy_literal(graph: Graph, node: URIRef, predicate: URIRef) -> Literal:
    values = list(graph.objects(node, predicate))
    if len(values) != 1:
        raise PolicyProfileError(f"{predicate}: expected exactly one policy value, found {len(values)}")
    value = values[0]
    if (not isinstance(value, Literal) or value.datatype != XSD.decimal
            or value.language is not None or value.ill_typed
            or not isinstance(value.toPython(), Decimal)
            or DECIMAL_LEXICAL.fullmatch(str(value)) is None):
        raise PolicyProfileError(f"{predicate}: expected a finite, well-formed xsd:decimal")
    try:
        finite = Decimal(str(value)).is_finite()
    except InvalidOperation:
        finite = False
    if not finite:
        raise PolicyProfileError(f"{predicate}: expected a finite, well-formed xsd:decimal")
    return value


def validate_policy_profile(graph: Graph, node: URIRef,
                            predicates: Iterable[URIRef]) -> None:
    # A fixed predicate order also fixes diagnostics for multiple input faults.
    for predicate in predicates:
        policy_literal(graph, node, predicate)


def validate_hvac_policy(graph: Graph) -> None:
    validate_policy_profile(graph, HVAC_POLICY_NODE, HVAC_POLICY_PREDICATES)


def validate_conflict_policy(policy: str) -> None:
    if policy != "first_writer_wins":
        raise ValueError(f"Unsupported conflict policy: {policy}")
