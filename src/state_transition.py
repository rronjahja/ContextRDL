from __future__ import annotations

from typing import Any

from rdflib import Graph, Literal, URIRef
from rdflib.namespace import XSD


BOOLEAN_PREDICATES = {
    "http://example.org/building#emergencyState",
}

DECIMAL_PREDICATES = {
    "http://example.org/building#currentSetpoint",
    "http://example.org/building#co2Level",
}


def make_literal(predicate: str, value: Any) -> Literal:
    if predicate in BOOLEAN_PREDICATES:
        if isinstance(value, str):
            coerced = value.strip().lower() == "true"
        else:
            coerced = bool(value)
        return Literal(coerced, datatype=XSD.boolean)

    if predicate in DECIMAL_PREDICATES:
        return Literal(float(value), datatype=XSD.decimal)

    return Literal(str(value))


def apply_action(graph: Graph, action: dict) -> Graph:
    new_graph = Graph()
    for triple in graph:
        new_graph.add(triple)

    zone = URIRef(action["zone"])
    predicate = URIRef(action["predicate"])

    for triple in list(new_graph.triples((zone, predicate, None))):
        new_graph.remove(triple)

    new_graph.add((zone, predicate, make_literal(action["predicate"], action["value"])))
    return new_graph


if __name__ == "__main__":
    graph = Graph()
    graph.parse("shapes/base_graph.ttl", format="turtle")

    action = {
        "zone": "http://example.org/building#ZoneA",
        "predicate": "http://example.org/building#currentSetpoint",
        "value": 23,
    }

    candidate = apply_action(graph, action)
    print("Triples in candidate graph:", len(candidate))