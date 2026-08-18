from __future__ import annotations

import random

from baseline_scheduler import nondeterministic_schedule
from dataset_builder import build_dataset, load_events, load_state
from admissibility import check_admissibility
from rule_engine import evaluate_rules, load_settings, resolve_governance_context
from rule_loader import load_rules
from state_transition import apply_action
from trace import graph_digest


def run_blind_baseline_once(seed: int | None = None):
    settings = load_settings("configs/settings.json")
    context = resolve_governance_context(settings=settings, contexts_path="data/contexts.json")
    graph_t = load_state("shapes/base_graph.ttl")
    events = load_events("data/events.jsonl")
    rules = load_rules("configs/rules.json")

    dataset, window_meta = build_dataset(graph_t, events, settings=settings)
    enabled = evaluate_rules(dataset, rules, settings=settings, context=context, window_meta=window_meta)
    rng = random.Random(seed) if seed is not None else None
    schedule = nondeterministic_schedule(enabled, rng=rng)

    current_graph = graph_t
    for action in schedule:
        current_graph = apply_action(current_graph, action)

    return current_graph


def run_blind_baseline_experiment(runs: int = 30):
    outcomes = []
    for seed in range(runs):
        graph_next = run_blind_baseline_once(seed=seed)
        conforms, report = check_admissibility(graph_next, "shapes/invariants.ttl")
        outcomes.append(
            {
                "digest": graph_digest(graph_next),
                "admissible": conforms,
                "report": report,
            }
        )

    unique = sorted({outcome["digest"] for outcome in outcomes})
    admissible_runs = sum(1 for outcome in outcomes if outcome["admissible"])
    inadmissible_runs = runs - admissible_runs
    inadmissible_digests = sorted({outcome["digest"] for outcome in outcomes if not outcome["admissible"]})

    print("Blind apply-all ablation (not the main paper baseline)")
    print("Runs:", runs)
    print("Unique successor states:", len(unique))
    print("Admissible outcomes:", admissible_runs)
    print("Inadmissible outcomes:", inadmissible_runs)
    if len(unique) == 1:
        print("Blind baseline behaved deterministically")
    else:
        print("Blind baseline produced divergent outcomes")
        for digest in unique:
            print(" -", digest)
    if inadmissible_digests:
        print("Inadmissible successor digests:")
        for digest in inadmissible_digests:
            print(" -", digest)

    return {
        "runs": runs,
        "unique_successor_states": len(unique),
        "digests": unique,
        "admissible_runs": admissible_runs,
        "inadmissible_runs": inadmissible_runs,
        "inadmissible_digests": inadmissible_digests,
    }


if __name__ == "__main__":
    run_blind_baseline_experiment()
