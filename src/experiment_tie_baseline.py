from __future__ import annotations

import random

from baseline_scheduler import nondeterministic_tie_schedule
from dataset_builder import build_dataset, load_state
from experiment_helpers import tie_conflict_events
from resolver import resolve_actions
from rule_engine import evaluate_rules, load_settings, resolve_governance_context
from rule_loader import load_rules
from trace import graph_digest


BASELINE_PREFIX_KEYS = ("roleRank", "priority", "tsKey")


def run_tie_baseline_once(seed: int | None = None):
    settings = load_settings("configs/settings.json")
    context = resolve_governance_context(settings=settings, contexts_path="data/contexts.json")
    graph_t = load_state("shapes/base_graph.ttl")
    rules = load_rules("configs/rules.json")

    dataset, window_meta = build_dataset(graph_t, tie_conflict_events(), settings=settings)
    enabled = evaluate_rules(dataset, rules, settings=settings, context=context, window_meta=window_meta)
    rng = random.Random(seed) if seed is not None else None
    schedule = nondeterministic_tie_schedule(enabled, prefix_keys=BASELINE_PREFIX_KEYS, rng=rng)

    accepted, graph_next, decisions = resolve_actions(
        graph_t,
        schedule,
        shapes_path="shapes/invariants.ttl",
        settings=settings,
    )

    return graph_next, accepted, decisions


def run_tie_baseline_experiment(runs: int = 30):
    digests = []

    for seed in range(runs):
        graph_next, _, _ = run_tie_baseline_once(seed=seed)
        digests.append(graph_digest(graph_next))

    unique = sorted(set(digests))

    print("Runs:", runs)
    print("Unique successor states:", len(unique))

    if len(unique) == 1:
        print("Tie baseline behaved deterministically")
    else:
        print("Tie baseline produced divergent outcomes")
        for digest in unique:
            print(" -", digest)

    return {
        "runs": runs,
        "unique_successor_states": len(unique),
        "digests": unique,
    }


if __name__ == "__main__":
    run_tie_baseline_experiment()
