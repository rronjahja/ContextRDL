from __future__ import annotations

from baseline_scheduler import nondeterministic_schedule
from dataset_builder import build_dataset, load_events, load_state
from rule_engine import evaluate_rules, load_settings, resolve_governance_context
from rule_loader import load_rules
from state_transition import apply_action
from trace import graph_digest


def run_blind_baseline_once():
    settings = load_settings("configs/settings.json")
    context = resolve_governance_context(settings=settings, contexts_path="data/contexts.json")
    graph_t = load_state("shapes/base_graph.ttl")
    events = load_events("data/events.jsonl")
    rules = load_rules("configs/rules.json")

    dataset, window_meta = build_dataset(graph_t, events, settings=settings)
    enabled = evaluate_rules(dataset, rules, settings=settings, context=context, window_meta=window_meta)
    schedule = nondeterministic_schedule(enabled)

    current_graph = graph_t
    for action in schedule:
        current_graph = apply_action(current_graph, action)

    return current_graph


def run_blind_baseline_experiment(runs: int = 30):
    digests = []
    for _ in range(runs):
        graph_next = run_blind_baseline_once()
        digests.append(graph_digest(graph_next))

    unique = sorted(set(digests))
    print("Blind apply-all ablation (not the main paper baseline)")
    print("Runs:", runs)
    print("Unique successor states:", len(unique))
    if len(unique) == 1:
        print("Blind baseline behaved deterministically")
    else:
        print("Blind baseline produced divergent outcomes")
        for digest in unique:
            print(" -", digest)

    return {
        "runs": runs,
        "unique_successor_states": len(unique),
        "digests": unique,
    }


if __name__ == "__main__":
    run_blind_baseline_experiment()