import time
import csv

from dataset_builder import build_dataset, load_events, load_state
from rule_loader import load_rules
from rule_engine import evaluate_rules, schedule_actions
from resolver import resolve_actions


def run_scalability_experiment(sizes = [1, 10, 50, 100, 200, 400, 800, 1200, 2000]):

    # create CSV file and write header
    with open("results/scalability.csv", "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["actions", "time_seconds"])

    for size in sizes:

        G_t = load_state("data/base_graph.ttl")
        events = load_events("data/events.jsonl")
        rules = load_rules("configs/rules.json")

        dataset = build_dataset(G_t, events)

        Act_t = evaluate_rules(dataset, rules)

        # simulate larger concurrent rule instance sets
        Act_t = Act_t * size

        Sigma_t = schedule_actions(Act_t)

        start = time.time()

        B_t, G_next, decisions = resolve_actions(G_t, Sigma_t)

        end = time.time()

        elapsed = round(end - start, 4)

        print(
            "Actions:", len(Act_t),
            "Accepted:", len(B_t),
            "Time:", elapsed, "seconds"
        )

        # append result to CSV
        with open("results/scalability.csv", "a", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([len(Act_t), elapsed])


if __name__ == "__main__":
    run_scalability_experiment()