from __future__ import annotations

from dataset_builder import build_dataset, load_state
from experiment_helpers import duplicate_identity_events
from rule_engine import evaluate_rules, load_settings, resolve_governance_context
from rule_loader import load_rules


def run_identity_experiment():
    settings = load_settings("configs/settings.json")
    context = resolve_governance_context(settings=settings, contexts_path="data/contexts.json")
    graph_t = load_state("shapes/base_graph.ttl")
    rules = load_rules("configs/rules.json")

    dataset, window_meta = build_dataset(graph_t, duplicate_identity_events(), settings=settings)
    enabled = evaluate_rules(dataset, rules, settings=settings, context=context, window_meta=window_meta)

    aids = [action["aid"] for action in enabled if action["rid"] == "r1"]
    event_ids = [action["event_id"] for action in enabled if action["rid"] == "r1"]

    print("Triggered r1 action count:", len(aids))
    print("Event IDs:", event_ids)
    print("Action IDs:")
    for aid in aids:
        print(" -", aid)
    print("Unique action IDs:", len(set(aids)))

    return {
        "triggered_r1_actions": len(aids),
        "unique_action_ids": len(set(aids)),
        "event_ids": event_ids,
        "action_ids": aids,
    }


if __name__ == "__main__":
    run_identity_experiment()
