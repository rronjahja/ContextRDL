from __future__ import annotations

import json
from typing import Any, Dict, List

from admissibility import check_admissibility
from state_transition import apply_action
from trace import graph_digest, graph_from_snapshot, load_trace


def _accepted_decisions(trace: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [decision for decision in trace.get("decisions", []) if decision.get("accepted")]


def replay_trace(trace_path: str = "results/trace.json", shapes_path: str = "shapes/invariants.ttl") -> Dict[str, Any]:
    trace = load_trace(trace_path)

    current_graph = graph_from_snapshot(trace["input_graph"])
    input_digest_ok = graph_digest(current_graph) == trace["input_graph"]["digest"]

    accepted_decisions = _accepted_decisions(trace)
    step_results: List[Dict[str, Any]] = []

    for index, action in enumerate(trace["accepted_actions"]):
        recorded = accepted_decisions[index] if index < len(accepted_decisions) else {}
        pre_digest = graph_digest(current_graph)

        candidate = apply_action(current_graph, action)
        candidate_digest = graph_digest(candidate)
        conforms, report = check_admissibility(candidate, shapes_path)
        if not conforms:
            raise ValueError(
                f"Replay failed: accepted action {action['aid']} became inadmissible.\n{report}"
            )

        step_results.append(
            {
                "aid": action["aid"],
                "index": index,
                "pre_digest_match": pre_digest == recorded.get("pre_graph_digest", pre_digest),
                "post_digest_match": candidate_digest == recorded.get("post_graph_digest", candidate_digest),
            }
        )
        current_graph = candidate

    replay_digest = graph_digest(current_graph)
    expected_digest = trace["successor_graph"]["digest"]
    successor_digest_match = replay_digest == expected_digest
    step_digest_match = all(result["pre_digest_match"] and result["post_digest_match"] for result in step_results)

    summary = {
        "trace_version": trace.get("trace_version"),
        "input_digest_match": input_digest_ok,
        "accepted_action_count": len(trace["accepted_actions"]),
        "replay_successor_digest": replay_digest,
        "expected_successor_digest": expected_digest,
        "successor_digest_match": successor_digest_match,
        "step_digest_match": step_digest_match,
        "step_results": step_results,
    }
    return summary


if __name__ == "__main__":
    print(json.dumps(replay_trace(), indent=2, sort_keys=True))
