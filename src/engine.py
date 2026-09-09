from __future__ import annotations
import paths  # noqa: E402  (results layout)

import hashlib
import json
import os
from copy import deepcopy
from typing import Any, Dict, List, Optional

from dataset_builder import build_dataset, load_events, load_state
from resolver import resolve_actions
from rule_engine import evaluate_rules, load_settings, resolve_governance_context, schedule_actions
from rule_loader import load_rules, validate_rules
from trace import build_trace, file_sha256, save_trace


def _deep_update(base: Dict[str, Any], updates: Dict[str, Any]) -> Dict[str, Any]:
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            base[key] = _deep_update(dict(base[key]), value)
        else:
            base[key] = value
    return base


def run_engine(
    state_path: str = "data/base_graph.ttl",
    events_path: str = "data/events.jsonl",
    rules_path: str = "configs/rules.json",
    settings_path: str = "configs/settings.json",
    shapes_path: str = "shapes/invariants.ttl",
    contexts_path: str = "data/contexts.json",
    trace_path: str = paths.hvac_trace("trace.json"),
    save_trace_file: bool = True,
    settings_override: Optional[Dict[str, Any]] = None,
    state_graph=None,
    events: Optional[List[Dict[str, Any]]] = None,
    rules: Optional[List[Dict[str, Any]]] = None,
    context_name: Optional[str] = None,
    anchor_timestamp: Optional[str] = None,
    extra_window_meta: Optional[Dict[str, Any]] = None,
):
    settings = load_settings(settings_path)
    if settings_override:
        settings = _deep_update(settings, deepcopy(settings_override))
    # The validator implementation actually used by check_admissibility is
    # selected by the ADMISSIBILITY_REGIME environment variable. It is part of
    # the execution configuration (Definition 11 (i)), so the effective value
    # is recorded in the trace settings and replay_full re-applies it.
    settings["admissibility_regime"] = os.environ.get("ADMISSIBILITY_REGIME", "incremental").lower()

    current_state = deepcopy(state_graph) if state_graph is not None else load_state(state_path)
    event_list = deepcopy(events) if events is not None else load_events(events_path)
    rule_list = deepcopy(rules) if rules is not None else load_rules(rules_path)
    validate_rules(rule_list)
    # The execution configuration records the rule set itself (canonical,
    # rid-sorted, so that R is a set) and the content identity of the shape
    # graph (Definition 11 (iii), (xi)); replay rebuilds the rules from the
    # trace and verifies the shape graph before comparing.
    canonical_rules = sorted(deepcopy(rule_list), key=lambda r: r["rid"])
    canonical_json = json.dumps(canonical_rules, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    settings["dependencies"] = {
        "rules_sha256": hashlib.sha256(canonical_json.encode("utf-8")).hexdigest(),
        "rules_inline": canonical_rules,
        "shapes_path": shapes_path, "shapes_sha256": file_sha256(shapes_path),
        "binding_profile": "nt-1",
    }
    rule_provenance = ({"rules_source": "file", "rules_path": rules_path, "rules_file_sha256": file_sha256(rules_path)}
                       if rules is None else {"rules_source": "inline"})

    governance_context = resolve_governance_context(
        settings=settings,
        contexts_path=contexts_path,
        context_name=context_name,
    )

    dataset, window_meta = build_dataset(
        current_state,
        event_list,
        settings=settings,
        anchor_timestamp=anchor_timestamp,
    )
    window_meta = dict(window_meta)
    window_meta["governance_context"] = governance_context
    window_meta["event_delivery"] = settings.get("event_delivery", "redelivery")
    if extra_window_meta:
        window_meta.update(deepcopy(extra_window_meta))

    enabled_actions = evaluate_rules(
        dataset,
        rule_list,
        settings=settings,
        context=governance_context,
        window_meta=window_meta,
    )
    schedule = schedule_actions(enabled_actions, settings=settings)
    resolver_settings = deepcopy(settings)
    resolver_settings.setdefault("governance", {})
    resolver_settings["governance"]["active_roles"] = governance_context.get("active_roles")
    resolver_settings["governance"]["enforce_active_roles"] = governance_context.get(
        "enforce_active_roles", False
    )
    accepted_actions, successor_graph, decisions = resolve_actions(
        current_state,
        schedule,
        shapes_path=shapes_path,
        settings=resolver_settings,
    )

    trace = build_trace(
        input_graph=current_state,
        enabled_actions=enabled_actions,
        schedule=schedule,
        accepted_actions=accepted_actions,
        successor_graph=successor_graph,
        decisions=decisions,
        settings=settings,
        window_meta=window_meta,
        rules=rule_list,
        events=window_meta.get("selected_events", event_list),
    )
    trace.setdefault("environment", {}).update(rule_provenance)

    if save_trace_file:
        save_trace(trace, trace_path)

    return current_state, schedule, accepted_actions, successor_graph, trace


if __name__ == "__main__":
    graph_t, schedule, accepted, graph_next, trace = run_engine()

    print("\nSchedule Σ_t:\n")
    for action in schedule:
        print(action["rid"], action["aid"], action["target_key"], action["value"])

    print("\nAccepted actions B_t:\n")
    for action in accepted:
        print(action["rid"], action["aid"], action["target_key"], action["value"])

    print("\nTrace summary:\n")
    print(trace["summary"])
    print("Successor digest:", trace["successor_graph"]["digest"])
