from __future__ import annotations

import json
from typing import Any, Dict, List

from admissibility import check_admissibility
from state_transition import apply_action
from trace import graph_digest, graph_from_snapshot, load_trace


def replay_trace(trace_path: str = "results/trace.json", shapes_path: str = "shapes/invariants.ttl") -> Dict[str, Any]:
    trace = load_trace(trace_path)

    current_graph = graph_from_snapshot(trace["input_graph"])
    input_digest_ok = graph_digest(current_graph) == trace["input_graph"]["digest"]

    accepted_actions: List[Dict[str, Any]] = trace.get("accepted_actions", [])
    accepted_decisions = [decision for decision in trace.get("decisions", []) if decision.get("accepted")]
    if len(accepted_actions) != len(accepted_decisions):
        raise ValueError(
            "Replay failed: accepted action count does not match accepted decision count "
            f"({len(accepted_actions)} != {len(accepted_decisions)})."
        )

    step_digest_match = True
    step_results = []

    for index, (action, decision) in enumerate(zip(accepted_actions, accepted_decisions)):
        pre_digest = graph_digest(current_graph)
        if decision.get("pre_graph_digest") != pre_digest:
            step_digest_match = False

        candidate = apply_action(current_graph, action)
        conforms, report = check_admissibility(candidate, shapes_path)
        if not conforms:
            raise ValueError(
                f"Replay failed: accepted action {action['aid']} became inadmissible.\n{report}"
            )

        post_digest = graph_digest(candidate)
        if decision.get("post_graph_digest") != post_digest:
            step_digest_match = False

        step_results.append(
            {
                "index": index,
                "aid": action["aid"],
                "pre_digest_match": decision.get("pre_graph_digest") == pre_digest,
                "post_digest_match": decision.get("post_graph_digest") == post_digest,
            }
        )
        current_graph = candidate

    replay_digest = graph_digest(current_graph)
    expected_digest = trace["successor_graph"]["digest"]
    successor_digest_match = replay_digest == expected_digest

    summary = {
        "trace_version": trace.get("trace_version"),
        "input_digest_match": input_digest_ok,
        "accepted_action_count": len(accepted_actions),
        "replay_successor_digest": replay_digest,
        "expected_successor_digest": expected_digest,
        "successor_digest_match": successor_digest_match,
        "step_digest_match": step_digest_match,
        "step_results": step_results,
    }
    return summary


if __name__ == "__main__":
    print(json.dumps(replay_trace(), indent=2, sort_keys=True))