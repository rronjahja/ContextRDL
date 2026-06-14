from __future__ import annotations

import hashlib
import json
import os
from copy import deepcopy
from typing import Any, Dict, Iterable, List, Mapping, Tuple
from pathlib import Path
from rdflib import Graph


def canonical_triple_lines(graph: Graph) -> List[str]:
    return sorted(f"{s.n3()} {p.n3()} {o.n3()} ." for s, p, o in graph)


def graph_digest(graph: Graph) -> str:
    payload = "\n".join(canonical_triple_lines(graph))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def graph_delta(before: Graph, after: Graph) -> Tuple[List[str], List[str]]:
    before_lines = set(canonical_triple_lines(before))
    after_lines = set(canonical_triple_lines(after))
    removed = sorted(before_lines - after_lines)
    inserted = sorted(after_lines - before_lines)
    return removed, inserted


def serialize_graph_snapshot(graph: Graph) -> Dict[str, Any]:
    lines = canonical_triple_lines(graph)
    return {
        "triples": lines,
        "digest": hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest(),
        "triple_count": len(lines),
    }


def graph_from_snapshot(snapshot: Mapping[str, Any]) -> Graph:
    graph = Graph()
    triples = snapshot.get("triples", [])
    if triples:
        graph.parse(data="\n".join(triples), format="nt")
    return graph


def _jsonable_action(action: Mapping[str, Any]) -> Dict[str, Any]:
    return json.loads(json.dumps(action, sort_keys=True, default=str))


def _rules_snapshot(rules: Iterable[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    snapshot: List[Dict[str, Any]] = []
    for rule in rules:
        snapshot.append(
            {
                "rid": rule.get("rid"),
                "priority": rule.get("priority"),
                "issuing_role": rule.get("issuing_role"),
                "predicate": (rule.get("insert_template") or {}).get("predicate"),
                "value_expr": deepcopy((rule.get("insert_template") or {}).get("value_expr")),
            }
        )
    return snapshot


def build_trace(
    input_graph: Graph,
    enabled_actions: Iterable[Mapping[str, Any]],
    schedule: Iterable[Mapping[str, Any]],
    accepted_actions: Iterable[Mapping[str, Any]],
    successor_graph: Graph,
    decisions: Iterable[Mapping[str, Any]],
    settings: Mapping[str, Any] | None = None,
    window_meta: Mapping[str, Any] | None = None,
    rules: Iterable[Mapping[str, Any]] | None = None,
    events: Iterable[Mapping[str, Any]] | None = None,
) -> Dict[str, Any]:
    enabled_list = [_jsonable_action(action) for action in enabled_actions]
    schedule_list = [_jsonable_action(action) for action in schedule]
    accepted_list = [_jsonable_action(action) for action in accepted_actions]
    decisions_list = json.loads(json.dumps(list(decisions), sort_keys=True, default=str))

    trace = {
        "trace_version": "2.1",
        "settings": deepcopy(settings) if settings is not None else {},
        "window": deepcopy(window_meta) if window_meta is not None else {},
        "rules": _rules_snapshot(rules or []),
        "events": json.loads(json.dumps(list(events or []), sort_keys=True, default=str)),
        "input_graph": serialize_graph_snapshot(input_graph),
        "enabled_actions": enabled_list,
        "schedule": schedule_list,
        "accepted_actions": accepted_list,
        "decisions": decisions_list,
        "successor_graph": serialize_graph_snapshot(successor_graph),
        "summary": {
            "enabled_count": len(enabled_list),
            "scheduled_count": len(schedule_list),
            "accepted_count": len(accepted_list),
            "rejected_count": len(schedule_list) - len(accepted_list),
        },
    }
    return trace


def save_trace(trace: Mapping[str, Any], path: str = "results/trace.json") -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(trace, handle, indent=2, sort_keys=True)


def load_trace(path: str = "results/trace.json") -> Dict[str, Any]:
    p = Path(path)
    if not p.is_absolute() and not p.exists():
        p = Path(__file__).resolve().parent.parent / p
    with open(p, "r", encoding="utf-8") as handle:
        return json.load(handle)


if __name__ == "__main__":
    graph = Graph()
    graph.parse("base_graph.ttl", format="turtle")
    snapshot = serialize_graph_snapshot(graph)
    rebuilt = graph_from_snapshot(snapshot)

    print("Original digest:", graph_digest(graph))
    print("Rebuilt digest:", graph_digest(rebuilt))
