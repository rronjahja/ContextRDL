"""
Full execution replay.

The existing ``replay.py`` in the project only verifies that re-applying the
recorded accepted actions to the recorded input graph yields the recorded
successor digest.  That is a state check, not an execution check: it does not
reconstruct enabled actions, the schedule, or the decisions -- so changes in
rule evaluation or scheduling that happen to leave the final state intact
would replay "successfully" despite producing a different execution.

``replay_full`` re-runs the whole pipeline:

    input_graph + events + rules + settings + context
        -> build_dataset -> evaluate_rules -> schedule_actions -> resolve_actions

and compares the regenerated artifacts against the recorded ones:
    enabled aids   == recorded enabled aids
    schedule aids  == recorded schedule aids
    decision keys (aid, accepted, reason) == recorded
    accepted aids  == recorded accepted aids
    successor digest == recorded successor digest

Returns a structured report.  Any mismatch indicates a divergence somewhere
in the pipeline -- typically evaluate_rules or the scheduling key.
"""
from __future__ import annotations

import hashlib
import json
import os
from copy import deepcopy
from typing import Any, Dict, List, Mapping

from rdflib import Graph

from dataset_builder import build_dataset
from resolver import resolve_actions as resolve_original
from rule_engine import evaluate_rules, resolve_governance_context, schedule_actions
from rule_loader import load_rules
from trace import graph_from_snapshot, load_trace


def _graph_digest(graph: Graph) -> str:
    lines = sorted(f"{s.n3()} {p.n3()} {o.n3()} ." for s, p, o in graph)
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def _decision_keys(decisions: List[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    return [
        {
            "aid":      d.get("aid"),
            "rid":      d.get("rid"),
            "accepted": d.get("accepted"),
            "reason":   d.get("reason"),
        }
        for d in decisions
    ]


def replay_full(
    trace_path: str = "results/trace.json",
    rules_path: str = "configs/rules.json",
    contexts_path: str = "data/contexts.json",
) -> Dict[str, Any]:
    trace = load_trace(trace_path)

    settings     = trace["settings"]
    window_meta  = dict(trace["window"])
    events       = trace["events"]
    input_graph  = graph_from_snapshot(trace["input_graph"])

    # Recorded artifacts we must regenerate and compare against
    recorded_enabled_aids   = [a["aid"] for a in trace["enabled_actions"]]
    recorded_schedule_aids  = [a["aid"] for a in trace["schedule"]]
    recorded_accepted_aids  = [a["aid"] for a in trace["accepted_actions"]]
    recorded_decision_keys  = _decision_keys(trace["decisions"])
    recorded_successor_dig  = trace["successor_graph"]["digest"]

    # Rebuild the rules from the canonical file (trace stores only a snapshot)
    rules = load_rules(rules_path)

    # Rebuild the governance context that was active when the trace was recorded.
    # The trace stores the fully-resolved governance_context; the settings by
    # themselves can be ambiguous.  We honour what was recorded.
    recorded_context = window_meta.get("governance_context") or \
                       resolve_governance_context(settings=settings, contexts_path=contexts_path)

    # Re-run the pipeline
    dataset, regenerated_meta = build_dataset(
        input_graph, events, settings=settings,
        anchor_timestamp=window_meta.get("anchor_timestamp"),
    )

    enabled  = evaluate_rules(
        dataset, rules, settings=settings,
        context=recorded_context, window_meta=regenerated_meta,
    )
    schedule = schedule_actions(enabled, settings=settings)
    accepted, successor, decisions = resolve_original(
        input_graph, schedule,
        shapes_path=(settings.get("paths", {}) or {}).get("shapes", "shapes/invariants.ttl"),
        settings=settings,
    )

    # Compare
    regen_enabled_aids  = [a["aid"] for a in enabled]
    regen_schedule_aids = [a["aid"] for a in schedule]
    regen_accepted_aids = [a["aid"] for a in accepted]
    regen_decision_keys = _decision_keys(decisions)
    regen_successor_dig = _graph_digest(successor)

    report = {
        "enabled_match":   sorted(regen_enabled_aids)  == sorted(recorded_enabled_aids),
        "schedule_match":  regen_schedule_aids        == recorded_schedule_aids,
        "accepted_match":  regen_accepted_aids        == recorded_accepted_aids,
        "decisions_match": regen_decision_keys        == recorded_decision_keys,
        "digest_match":    regen_successor_dig        == recorded_successor_dig,
    }
    report["overall_pass"] = all(report.values())
    report["regenerated"] = {
        "enabled_count":   len(regen_enabled_aids),
        "schedule_count":  len(regen_schedule_aids),
        "accepted_count":  len(regen_accepted_aids),
        "successor_digest": regen_successor_dig,
    }
    report["recorded"] = {
        "enabled_count":   len(recorded_enabled_aids),
        "schedule_count":  len(recorded_schedule_aids),
        "accepted_count":  len(recorded_accepted_aids),
        "successor_digest": recorded_successor_dig,
    }

    # If anything failed, include the first diverging element for each axis
    diffs: Dict[str, Any] = {}
    if not report["schedule_match"]:
        for i, (a, b) in enumerate(zip(regen_schedule_aids, recorded_schedule_aids)):
            if a != b:
                diffs["first_schedule_diff"] = {"index": i, "regen": a, "recorded": b}
                break
    if not report["decisions_match"]:
        for i, (a, b) in enumerate(zip(regen_decision_keys, recorded_decision_keys)):
            if a != b:
                diffs["first_decision_diff"] = {"index": i, "regen": a, "recorded": b}
                break
    if diffs:
        report["diffs"] = diffs

    return report


if __name__ == "__main__":
    import sys
    trace_path = sys.argv[1] if len(sys.argv) > 1 else "results/trace.json"
    report = replay_full(trace_path=trace_path)
    print(json.dumps(report, indent=2))
    sys.exit(0 if report["overall_pass"] else 1)
