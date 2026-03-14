from rdflib import Graph, URIRef, Literal
from rdflib.namespace import XSD
from admissibility import check_admissibility


BOOLEAN_PREDICATES = {
    "http://example.org/building#emergencyState"
}

DECIMAL_PREDICATES = {
    "http://example.org/building#currentSetpoint",
    "http://example.org/building#co2Level"
}


def make_literal(predicate, value):
    if predicate in BOOLEAN_PREDICATES:
        return Literal(str(value).lower() == "true", datatype=XSD.boolean)

    if predicate in DECIMAL_PREDICATES:
        return Literal(float(value), datatype=XSD.decimal)

    return Literal(value)


def apply_action(graph, action):
    new_graph = Graph()

    for triple in graph:
        new_graph.add(triple)

    zone = URIRef(action["zone"])
    predicate = URIRef(action["predicate"])

    for s, p, o in list(new_graph.triples((zone, predicate, None))):
        new_graph.remove((s, p, o))

    value = make_literal(action["predicate"], action["value"])
    new_graph.add((zone, predicate, value))

    return new_graph


if __name__ == "__main__":
    g = Graph()
    g.parse("data/base_graph.ttl", format="turtle")

    action = {
        "zone": "http://example.org/building#ZoneA",
        "predicate": "http://example.org/building#currentSetpoint",
        "value": "24"
    }

    candidate = apply_action(g, action)

    conforms, report = check_admissibility(
        candidate,
        "shapes/invariants.ttl"
    )

    print("Candidate admissible:", conforms)