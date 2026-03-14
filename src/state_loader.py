from rdflib import Graph


def load_state(path):
    g = Graph()
    g.parse(path, format="turtle")
    return g


if __name__ == "__main__":
    g = load_state("data/base_graph.ttl")

    print("Triples in graph:", len(g))

    for s, p, o in g:
        print(s, p, o)