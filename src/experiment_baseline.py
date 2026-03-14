from baseline_scheduler import nondeterministic_schedule
from dataset_builder import build_dataset, load_events, load_state
from rule_loader import load_rules
from rule_engine import evaluate_rules
from state_transition import apply_action


def run_baseline_once():

    G_t = load_state("data/base_graph.ttl")

    events = load_events("data/events.jsonl")

    rules = load_rules("configs/rules.json")

    dataset = build_dataset(G_t, events)

    Act_t = evaluate_rules(dataset, rules)

    Sigma_t = nondeterministic_schedule(Act_t)

    current_graph = G_t

    for action in Sigma_t:
        current_graph = apply_action(current_graph, action)

    return current_graph


def run_baseline_experiment(runs=30):

    digests = []

    for _ in range(runs):

        G_next = run_baseline_once()

        triples = sorted([str(t) for t in G_next])
        digest = hash("\n".join(triples))

        digests.append(digest)

    unique = set(digests)

    print("Runs:", runs)
    print("Unique successor states:", len(unique))

    if len(unique) == 1:
        print("Baseline behaved deterministically")
    else:
        print("Baseline produced divergent outcomes")


if __name__ == "__main__":
    run_baseline_experiment()