from rdflib import Graph
from state_transition import apply_action
from admissibility import check_admissibility


def resolve_actions(graph, schedule):
    B_t = []
    current_graph = graph
    decisions = []

    for action in schedule:
        candidate_graph = apply_action(current_graph, action)

        conforms, report = check_admissibility(
            candidate_graph,
            "shapes/invariants.ttl"
        )

        if conforms:
            B_t.append(action)
            current_graph = candidate_graph
            decisions.append({
                "aid": action["aid"],
                "rid": action["rid"],
                "accepted": True,
                "reason": "admissible"
            })
        else:
            decisions.append({
                "aid": action["aid"],
                "rid": action["rid"],
                "accepted": False,
                "reason": report
            })

    return B_t, current_graph, decisions


if __name__ == "__main__":
    g = Graph()
    g.parse("data/base_graph.ttl", format="turtle")

    schedule = [
        {
            "aid": "test",
            "rid": "r1",
            "zone": "http://example.org/building#ZoneA",
            "predicate": "http://example.org/building#currentSetpoint",
            "value": "24"
        }
    ]

    B_t, G_next, decisions = resolve_actions(g, schedule)

    print("Accepted actions B_t:")
    for a in B_t:
        print(a)

    print("Decisions:")
    for d in decisions:
        print(d)

    print("Triples in successor state:", len(G_next))