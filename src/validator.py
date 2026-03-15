from __future__ import annotations

from rdflib import Graph

from admissibility import check_admissibility


def validate_graph(data_graph_path: str, shapes_graph_path: str):
    data_graph = Graph()
    data_graph.parse(data_graph_path, format="turtle")
    return check_admissibility(data_graph, shapes_graph_path)


if __name__ == "__main__":
    conforms, report = validate_graph("shapes/base_graph.ttl", "shapes/invariants.ttl")
    print("Conforms:", conforms)
    print(report)