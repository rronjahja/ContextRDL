from __future__ import annotations

import time
from typing import Dict, List

from dataset_builder import build_dataset, load_state
from resolver import resolve_actions
from rule_engine import evaluate_rules, load_settings, resolve_governance_context, schedule_actions
from rule_loader import load_rules


ZONE_B = "http://example.org/building#ZoneB"


def generate_valid_same_target_collision_events(action_count: int) -> List[Dict[str, object]]:
    timestamp = "2026-03-14T09:00:00Z"
    targets = [21.0, 22.0, 23.0]
    events: List[Dict[str, object]] = []

    for index in range(action_count):
        events.append(
            {
                "eid": f"coll-{index + 1:05d}",
                "timestamp": timestamp,
                "type": "PreheatRequest",
                "role": "operator",
                "payload": {
                    "zone": ZONE_B,
                    "target": targets[index % len(targets)],
                },
            }
        )

    return events


def run_collision_case(action_count: int):
    settings = load_settings("configs/settings.json")
    context = resolve_governance_context(settings=settings, contexts_path="data/contexts.json")
    graph_t = load_state("shapes/base_graph.ttl")
    rules = load_rules("configs/rules.json")
    events = generate_valid_same_target_collision_events(action_count)

    dataset, window_meta = build_dataset(graph_t, events, settings=settings)
    enabled = evaluate_rules(dataset, rules, settings=settings, context=context, window_meta=window_meta)
    schedule = schedule_actions(enabled, settings=settings)

    start = time.perf_counter()
    accepted, graph_next, decisions = resolve_actions(
        graph_t,
        schedule,
        shapes_path="shapes/invariants.ttl",
        settings=settings,
    )
    elapsed = time.perf_counter() - start

    reasons: Dict[str, int] = {}
    for decision in decisions:
        if decision.get("accepted"):
            continue
        reason = str(decision.get("reason", "unknown"))
        reasons[reason] = reasons.get(reason, 0) + 1

    winning_action = accepted[0] if accepted else None

    print(
        "Actions:", len(enabled),
        "Accepted:", len(accepted),
        "Rejected:", len(enabled) - len(accepted),
        "Time:", round(elapsed, 6), "seconds",
    )
    if winning_action is not None:
        print(
            "Winning action:",
            winning_action["rid"],
            winning_action["aid"],
            winning_action["target_key"],
            winning_action["value"],
        )
    if reasons:
        print("Rejection reasons:")
        for key, value in sorted(reasons.items()):
            print("-", key + ":", value)
    else:
        print("Rejection reasons: none")

    return {
        "actions": len(enabled),
        "accepted": len(accepted),
        "rejected": len(enabled) - len(accepted),
        "time_seconds": round(elapsed, 6),
        "winning_action": winning_action,
        "reasons": reasons,
    }


def run_collision_experiment(sizes=None):
    sizes = sizes or [10, 50, 100, 200, 500, 1000]
    results = []

    for size in sizes:
        print()
        print("Same-target collision workload with", size, "valid concurrent actions")
        result = run_collision_case(size)
        results.append(result)

    print()
    print("Summary")
    for result in results:
        print(
            "Actions:", result["actions"],
            "| Accepted:", result["accepted"],
            "| Rejected:", result["rejected"],
            "| Time:", result["time_seconds"], "seconds",
        )

    return results


if __name__ == "__main__":
    run_collision_experiment()