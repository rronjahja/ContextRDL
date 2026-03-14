from baseline_scheduler import nondeterministic_schedule
from dataset_builder import build_dataset, load_events, load_state
from rule_loader import load_rules
from rule_engine import evaluate_rules
from resolver import resolve_actions
from trace import graph_digest


def run_tie_baseline_once():
    G_t = load_state("data/base_graph.ttl")
    events = load_events("data/events.jsonl")
    rules = load_rules("configs/rules.json")

    dataset = build_dataset(G_t, events)
    Act_t = evaluate_rules(dataset, rules)

    Sigma_t = nondeterministic_schedule(Act_t)

    B_t, G_next, decisions = resolve_actions(G_t, Sigma_t)

    return G_next, B_t, decisions


def run_tie_baseline_experiment(runs=30):
    digests = []

    for _ in range(runs):
        G_next, _, _ = run_tie_baseline_once()
        digests.append(graph_digest(G_next))

    unique = set(digests)

    print("Runs:", runs)
    print("Unique successor states:", len(unique))

    if len(unique) == 1:
        print("Baseline behaved deterministically")
    else:
        print("Baseline produced divergent outcomes")


if __name__ == "__main__":
    run_tie_baseline_experiment()
