from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, List, Optional

from rdflib import Graph

from dataset_builder import build_dataset, load_events, load_state
from resolver import resolve_actions
from rule_engine import evaluate_rules, load_settings, resolve_governance_context, schedule_actions
from rule_loader import load_rules
from trace import build_trace, save_trace


def _deep_update(base: Dict[str, Any], updates: Dict[str, Any]) -> Dict[str, Any]:
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            base[key] = _deep_update(dict(base[key]), value)
        else:
            base[key] = value
    return base


def _copy_graph(graph: Graph) -> Graph:
    new_graph = Graph()
    for triple in graph:
        new_graph.add(triple)
    return new_graph


def run_engine(
    state_path: str = "shapes/base_graph.ttl",
    events_path: str = "data/events.jsonl",
    rules_path: str = "configs/rules.json",
    settings_path: str = "configs/settings.json",
    shapes_path: str = "shapes/invariants.ttl",
    contexts_path: str = "data/contexts.json",
    trace_path: str = "results/trace.json",
    save_trace_file: bool = True,
    settings_override: Optional[Dict[str, Any]] = None,
    state_graph: Optional[Graph] = None,
    events: Optional[List[Dict[str, Any]]] = None,
    rules: Optional[List[Dict[str, Any]]] = None,
    context_name: Optional[str] = None,
    anchor_timestamp: Optional[str] = None,
):
    settings = load_settings(settings_path)
    if settings_override:
        settings = _deep_update(settings, deepcopy(settings_override))

    current_state = _copy_graph(state_graph) if state_graph is not None else load_state(state_path)
    event_list = deepcopy(events) if events is not None else load_events(events_path)
    rule_list = deepcopy(rules) if rules is not None else load_rules(rules_path)

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

    enabled_actions = evaluate_rules(
        dataset,
        rule_list,
        settings=settings,
        context=governance_context,
        window_meta=window_meta,
    )
    schedule = schedule_actions(enabled_actions, settings=settings)
    accepted_actions, successor_graph, decisions = resolve_actions(
        current_state,
        schedule,
        shapes_path=shapes_path,
        settings=settings,
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
    print("Execution digest:", trace["execution_digest"])
    print("Successor digest:", trace["successor_graph"]["digest"])