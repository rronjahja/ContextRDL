from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional

from rdflib import Literal, URIRef

from dataset_builder import WINDOW_ALIAS_IRI, parse_timestamp, prop_uri


DEFAULT_SCHEDULE_KEY = [
    "roleRank",
    "priority",
    "tsKey",
    "rid",
    "bindKey",
    "aid",
]


def load_settings(path: str = "configs/settings.json") -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def load_contexts(path: str = "data/contexts.json") -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def resolve_governance_context(
    settings: Optional[Dict[str, Any]] = None,
    contexts_path: str = "data/contexts.json",
    context_name: Optional[str] = None,
) -> Dict[str, Any]:
    settings = settings or {}

    try:
        contexts = load_contexts(contexts_path)
    except FileNotFoundError:
        contexts = {}

    governance_cfg = settings.get("governance", {})
    selected_name = context_name or governance_cfg.get("context_name") or "default"
    context = contexts.get(selected_name, {})

    role_rank = dict(context.get("role_rank", {}))
    role_rank.update(settings.get("role_precedence", {}))

    active_roles = context.get("active_roles") or list(role_rank.keys())

    return {
        "name": selected_name,
        "principal": context.get("principal", "automation-engine"),
        "active_roles": active_roles,
        "role_rank": role_rank,
        "enforce_active_roles": bool(governance_cfg.get("enforce_active_roles", False)),
    }


def _json_default(value: Any) -> Any:
    if isinstance(value, (set, tuple)):
        return list(value)
    return str(value)


def _stable_json(data: Any) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), default=_json_default)


def _row_to_bindings(row: Any) -> Dict[str, Any]:
    bindings: Dict[str, Any] = {}
    for key, value in row.asdict().items():
        if isinstance(value, Literal):
            bindings[key] = value.toPython()
        elif isinstance(value, URIRef):
            bindings[key] = str(value)
        else:
            bindings[key] = str(value)
    return bindings


def _timestamp_key(value: str) -> int:
    dt = parse_timestamp(value)
    return int(dt.astimezone(timezone.utc).timestamp() * 1_000_000)


def _numeric(value: Any) -> float:
    return float(value)


def _lookup_event_role(dataset: Any, event_uri: str) -> Optional[str]:
    if not event_uri:
        return None
    try:
        alias_graph = dataset.graph(WINDOW_ALIAS_IRI)
        role_value = alias_graph.value(URIRef(event_uri), prop_uri("role"))
    except Exception:
        return None
    if role_value is None:
        return None
    if isinstance(role_value, Literal):
        return str(role_value.toPython())
    return str(role_value)


def build_action_value(rule: Mapping[str, Any], bindings: Mapping[str, Any]) -> Any:
    insert_template = rule.get("insert_template", {})
    value_expr = insert_template.get("value_expr")
    if value_expr is None:
        raise ValueError(f"Rule {rule.get('rid')} is missing insert_template.value_expr")

    kind = value_expr.get("kind")

    if kind == "copy":
        return bindings[value_expr["var"]]

    if kind == "literal":
        return value_expr["value"]

    if kind == "numeric_add":
        left = _numeric(bindings[value_expr["left"]])
        right = _numeric(bindings[value_expr["right"]])
        return left + right

    if kind == "numeric_add_constant":
        left = _numeric(bindings[value_expr["var"]])
        constant = _numeric(value_expr["constant"])
        return left + constant

    raise ValueError(f"Unsupported value expression kind: {kind}")


def create_action(
    rule: Mapping[str, Any],
    binding_row: Any,
    settings: Optional[Dict[str, Any]] = None,
    context: Optional[Dict[str, Any]] = None,
    window_id: str = "urn:window",
    event_role: Optional[str] = None,
) -> Dict[str, Any]:
    settings = settings or {}
    context = context or resolve_governance_context(settings)
    bindings = _row_to_bindings(binding_row)

    predicate = rule["insert_template"]["predicate"]
    rid = rule["rid"]
    role = rule["issuing_role"]

    event_uri = str(bindings.get("e", ""))
    event_id = str(bindings.get("eid") or event_uri.rsplit(":", 1)[-1])
    event_ts = str(bindings.get("ts", ""))
    zone = str(bindings["zone"])

    role_rank = int(context.get("role_rank", {}).get(role, settings.get("role_precedence", {}).get(role, 999)))
    ts_key = _timestamp_key(event_ts) if event_ts else 0

    bind_key_payload = {key: bindings[key] for key in sorted(bindings) if key not in {"e"}}
    bind_key = _stable_json(bind_key_payload)

    aid_payload = {
        "rid": rid,
        "eid": event_id,
        "window_id": window_id,
        "predicate": predicate,
        "bindings": bind_key_payload,
    }
    aid = hashlib.sha256(_stable_json(aid_payload).encode("utf-8")).hexdigest()

    value = build_action_value(rule, bindings)

    action = {
        "aid": aid,
        "rid": rid,
        "event_uri": event_uri,
        "event_id": event_id,
        "event_ts": event_ts,
        "event_role": event_role,
        "window_id": window_id,
        "zone": zone,
        "predicate": predicate,
        "target_key": f"{zone}|{predicate}",
        "value": value,
        "priority": int(rule["priority"]),
        "role": role,
        "roleRank": role_rank,
        "tsKey": ts_key,
        "bindKey": bind_key,
        "bindings": bind_key_payload,
    }
    return action


def evaluate_rules(
    dataset: Any,
    rules: Iterable[Mapping[str, Any]],
    settings: Optional[Dict[str, Any]] = None,
    context: Optional[Dict[str, Any]] = None,
    window_meta: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    settings = settings or {}
    context = context or resolve_governance_context(settings)
    window_id = (window_meta or {}).get("window_id", "urn:window")
    active_roles = set(context.get("active_roles", []))

    actions: List[Dict[str, Any]] = []
    seen_aids = set()

    for rule in rules:
        rule_role = rule["issuing_role"]
        if context.get("enforce_active_roles") and rule_role not in active_roles:
            continue

        results = dataset.query(rule["condition_select"])
        for row in results:
            bindings = _row_to_bindings(row)
            event_uri = str(bindings.get("e", ""))
            event_role = _lookup_event_role(dataset, event_uri)

            if event_role is not None and event_role != rule_role:
                continue
            if context.get("enforce_active_roles") and event_role is not None and event_role not in active_roles:
                continue

            action = create_action(
                rule,
                row,
                settings=settings,
                context=context,
                window_id=window_id,
                event_role=event_role,
            )
            if action["aid"] in seen_aids:
                continue
            seen_aids.add(action["aid"])
            actions.append(action)

    return actions


def _schedule_component(action: Mapping[str, Any], field: str) -> Any:
    if field not in action:
        raise KeyError(f"Schedule key field '{field}' is missing from action {action.get('aid')}")
    return action[field]


def schedule_actions(actions: List[Mapping[str, Any]], settings: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    settings = settings or {}
    schedule_key = settings.get("schedule_key", DEFAULT_SCHEDULE_KEY)
    return sorted(
        (deepcopy(action) for action in actions),
        key=lambda action: tuple(_schedule_component(action, field) for field in schedule_key),
    )


if __name__ == "__main__":
    from dataset_builder import build_dataset, load_events, load_state
    from rule_loader import load_rules

    settings = load_settings("configs/settings.json")
    context = resolve_governance_context(settings=settings, contexts_path="data/contexts.json")
    state = load_state("shapes/base_graph.ttl")
    events = load_events("data/events.jsonl")
    rules = load_rules("configs/rules.json")

    dataset, meta = build_dataset(state, events, settings=settings)
    enabled = evaluate_rules(dataset, rules, settings=settings, context=context, window_meta=meta)
    schedule = schedule_actions(enabled, settings=settings)

    print("Context:")
    print(json.dumps(context, indent=2))
    print("Enabled actions:")
    for action in enabled:
        print(action)
    print("\nSchedule:")
    for action in schedule:
        print(action["rid"], action["aid"], action["target_key"])