from engine import run_engine
import json


def modify_role_rank(operator_rank, occupant_rank):
    with open("configs/settings.json", "r") as f:
        settings = json.load(f)

    settings["role_precedence"]["operator"] = operator_rank
    settings["role_precedence"]["occupant"] = occupant_rank

    with open("configs/settings.json", "w") as f:
        json.dump(settings, f, indent=2)


def run_case(name, operator_rank, occupant_rank):
    modify_role_rank(operator_rank, occupant_rank)

    _, Sigma_t, B_t, _, trace = run_engine()

    print(f"\n{name}")
    print("Role precedence:")
    print("operator:", operator_rank, "occupant:", occupant_rank)

    print("Schedule:")
    for a in Sigma_t:
        print(a["rid"], a["aid"])

    print("Accepted actions:")
    for a in B_t:
        print(a["rid"], a["aid"])

    print("Successor graph digest:")
    print(trace["successor_graph_digest"])

    print("Resolver decisions:")
    for d in trace["decisions"]:
        print(d["rid"], d["accepted"])


if __name__ == "__main__":
    run_case("Case 1: operator higher", 0, 1)
    run_case("Case 2: occupant higher", 1, 0)