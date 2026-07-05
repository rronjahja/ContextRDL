from dataset_builder import load_state


if __name__ == "__main__":
    graph = load_state("shapes/base_graph.ttl")
    print("Triples in graph:", len(graph))