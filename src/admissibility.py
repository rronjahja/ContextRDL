from rdflib import Graph
from pyshacl import validate


def check_admissibility(graph, shapes_path):

    shapes = Graph()
    shapes.parse(shapes_path, format="turtle")

    conforms, results_graph, results_text = validate(
        graph,
        shacl_graph=shapes,
        inference="none",
        abort_on_first=False,
        meta_shacl=False,
        debug=False,
    )

    return conforms, results_text


if __name__ == "__main__":

    g = Graph()
    g.parse("data/base_graph.ttl", format="turtle")

    conforms, report = check_admissibility(
        g,
        "shapes/invariants.ttl"
    )

    print("Admissible:", conforms)