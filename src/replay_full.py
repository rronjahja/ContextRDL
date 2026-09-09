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
import paths  # noqa: E402  (results layout)

import hashlib
import json
import os
from copy import deepcopy
from typing import Any, Dict, List, Mapping, Optional

from rdflib import Graph

from dataset_builder import build_dataset
from resolver import resolve_actions as resolve_original
from rule_engine import evaluate_rules, resolve_governance_context, schedule_actions
from rule_loader import load_rules
from trace import _jsonable_action, _rules_snapshot, file_sha256, graph_from_snapshot, load_trace
from rule_engine import BINDING_PROFILE


def _graph_digest(graph: Graph) -> str:
    lines = sorted(line for line in graph.serialize(format="nt").split("\n") if line)
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


# Free-text validator reports are not part of the compared decision record:
# their wording depends on the pySHACL version, not on the semantics.
_UNCOMPARED_DECISION_FIELDS = {"validation_report"}


def _record(action: Mapping[str, Any]) -> Dict[str, Any]:
    return json.loads(json.dumps(_jsonable_action(action), sort_keys=True, default=str))


def _action_records(actions: List[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    return sorted((_record(a) for a in actions), key=lambda a: a["aid"])


def _decision_keys(decisions: List[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """Every recorded decision field except the free-text validator report."""
    return [json.loads(json.dumps({k: v for k, v in d.items() if k not in _UNCOMPARED_DECISION_FIELDS},
                                  sort_keys=True, default=str)) for d in decisions]


def replay_full(
    trace_path: str = paths.hvac_trace("trace.json"),
    rules_path: Optional[str] = None,
    contexts_path: str = "data/contexts.json",
) -> Dict[str, Any]:
    trace = load_trace(trace_path)

    settings     = trace["settings"]
    # Replay uses the recorded execution configuration, including the
    # validator implementation that produced the trace. The process
    # environment is restored afterwards (see the finally block).
    previous_regime = os.environ.get("ADMISSIBILITY_REGIME")
    os.environ["ADMISSIBILITY_REGIME"] = str(settings.get("admissibility_regime", "incremental"))
    try:
        return _replay_body(trace, trace_path, rules_path, settings, contexts_path)
    finally:
        if previous_regime is None:
            os.environ.pop("ADMISSIBILITY_REGIME", None)
        else:
            os.environ["ADMISSIBILITY_REGIME"] = previous_regime


def _replay_body(trace, trace_path, rules_path, settings, contexts_path="data/contexts.json"):
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
    # and verify that the file is the one the trace was recorded with.
    deps = settings.get("dependencies", {})
    dependency_ok = True
    if deps.get("rules_inline") is not None:
        # The trace is self-contained: the canonical rule set is embedded.
        rules = deepcopy(deps["rules_inline"])
        canonical_json = json.dumps(sorted(rules, key=lambda r: r["rid"]), sort_keys=True,
                                    separators=(",", ":"), ensure_ascii=False)
        dependency_ok = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest() == deps.get("rules_sha256")
        if rules_path is not None:  # optional cross-check against a rule file
            dependency_ok = dependency_ok and sorted(load_rules(rules_path), key=lambda r: r["rid"]) == rules
    else:  # legacy traces without an embedded rule set
        rules_path = rules_path or deps.get("rules_path") or "configs/rules.json"
        rules = load_rules(rules_path)
        if deps.get("rules_sha256"):
            dependency_ok = dependency_ok and file_sha256(rules_path) == deps["rules_sha256"]
    shapes_path = deps.get("shapes_path") or (settings.get("paths", {}) or {}).get("shapes", "shapes/invariants.ttl")
    if deps.get("shapes_sha256"):
        dependency_ok = dependency_ok and file_sha256(shapes_path) == deps["shapes_sha256"]
    profile_ok = deps.get("binding_profile", BINDING_PROFILE) == BINDING_PROFILE
    # Internal consistency of the recorded snapshots.
    input_snapshot_ok = _graph_digest(input_graph) == trace["input_graph"]["digest"]
    recorded_successor_lines = trace["successor_graph"].get("triples")
    successor_snapshot_ok = True
    if recorded_successor_lines is not None:
        successor_snapshot_ok = hashlib.sha256("\n".join(sorted(recorded_successor_lines)).encode("utf-8")).hexdigest() \
            == trace["successor_graph"]["digest"]

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
    # Hand the recorded role context to the resolver exactly as engine.py does,
    # so gate (i) (role filter) replays under the recorded active-role set.
    resolver_settings = deepcopy(settings)
    resolver_settings.setdefault("governance", {})
    resolver_settings["governance"]["active_roles"] = recorded_context.get("active_roles")
    resolver_settings["governance"]["enforce_active_roles"] = recorded_context.get("enforce_active_roles", False)
    accepted, successor, decisions = resolve_original(
        input_graph, schedule,
        shapes_path=shapes_path,
        settings=resolver_settings,
    )

    # Compare
    regen_enabled_aids  = [a["aid"] for a in enabled]
    regen_schedule_aids = [a["aid"] for a in schedule]
    regen_accepted_aids = [a["aid"] for a in accepted]
    regen_decision_keys = _decision_keys(decisions)
    regen_successor_dig = _graph_digest(successor)

    regen_rules_snapshot = _rules_snapshot(rules)
    recorded_window_id = trace["window"].get("window_id")
    window_ids_in_actions = {a.get("window_id") for a in trace["enabled_actions"]} | {a.get("window_id") for a in trace["schedule"]}
    window_identity_ok = (recorded_window_id == regenerated_meta.get("window_id")
                          and window_ids_in_actions <= {recorded_window_id})
    report = {
        "window_identity_consistent": window_identity_ok,
        "dependencies_match": dependency_ok,
        "binding_profile_supported": profile_ok,
        "input_snapshot_consistent": input_snapshot_ok,
        "successor_snapshot_consistent": successor_snapshot_ok,
        "rules_snapshot_match": regen_rules_snapshot == trace.get("rules", regen_rules_snapshot),
        "enabled_match":   sorted(regen_enabled_aids)  == sorted(recorded_enabled_aids),
        "enabled_records_match": _action_records(enabled) == _action_records(trace["enabled_actions"]),
        "schedule_match":  regen_schedule_aids        == recorded_schedule_aids,
        "schedule_records_match": [_record(a) for a in schedule] == [_record(a) for a in trace["schedule"]],
        "accepted_match":  regen_accepted_aids        == recorded_accepted_aids,
        "accepted_records_match": [_record(a) for a in accepted] == [_record(a) for a in trace["accepted_actions"]],
        "decisions_match": regen_decision_keys        == recorded_decision_keys,
        "digest_match":    regen_successor_dig        == recorded_successor_dig,
        "successor_triples_match": recorded_successor_lines is None or
            sorted(recorded_successor_lines) == sorted(line for line in successor.serialize(format="nt").split("\n") if line),
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
    trace_path = sys.argv[1] if len(sys.argv) > 1 else paths.hvac_trace("trace.json")
    report = replay_full(trace_path=trace_path)
    print(json.dumps(report, indent=2))
    sys.exit(0 if report["overall_pass"] else 1)
