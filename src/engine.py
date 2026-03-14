from dataset_builder import build_dataset, load_events, load_state
from rule_loader import load_rules
from rule_engine import evaluate_rules, schedule_actions
from resolver import resolve_actions
from trace import build_trace, save_trace


def run_engine():
    G_t = load_state("data/base_graph.ttl")
    events = load_events("data/events.jsonl")
    rules = load_rules("configs/rules.json")

    dataset = build_dataset(G_t, events)

    Act_t = evaluate_rules(dataset, rules)
    Sigma_t = schedule_actions(Act_t)

    B_t, G_next, decisions = resolve_actions(G_t, Sigma_t)

    trace = build_trace(G_t, Act_t, Sigma_t, B_t, G_next)
    trace["decisions"] = decisions
    save_trace(trace)

    return G_t, Sigma_t, B_t, G_next, trace


if __name__ == "__main__":
    G_t, Sigma_t, B_t, G_next, trace = run_engine()

    print("\nTrace:\n")
    print(trace)

    print("\nSchedule Σ_t:\n")
    for a in Sigma_t:
        print(a["rid"], a["aid"])

    print("\nAccepted actions B_t:\n")
    for a in B_t:
        print(a["rid"], a["aid"])

    print("\nResolver decisions:\n")
    for d in trace["decisions"]:
        print(d["rid"], d["accepted"])

    print("\nSuccessor graph triples:", len(G_next))