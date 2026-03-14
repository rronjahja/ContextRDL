from rdflib import Graph
from pyshacl import validate


def validate_graph(data_graph_path, shapes_graph_path):
    data_graph = Graph()
    data_graph.parse(data_graph_path, format="turtle")

    shapes_graph = Graph()
    shapes_graph.parse(shapes_graph_path, format="turtle")

    conforms, results_graph, results_text = validate(
        data_graph,
        shacl_graph=shapes_graph,
        inference="none",
        abort_on_first=False,
        meta_shacl=False,
        debug=False,
    )

    return conforms, results_text


if __name__ == "__main__":
    conforms, report = validate_graph(
        "data/base_graph.ttl",
        "shapes/invariants.ttl"
    )

    print("Conforms:", conforms)
    print(report)