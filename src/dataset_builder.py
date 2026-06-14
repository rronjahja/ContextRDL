from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from pathlib import Path
from rdflib import Dataset, Graph, Literal, URIRef
from rdflib.namespace import XSD


STATE_GRAPH_IRI = URIRef("urn:state")
WINDOW_ALIAS_IRI = URIRef("urn:window")
URN_PROP_NS = "urn:prop:"

NUMERIC_PAYLOAD_KEYS = {"delta", "cap", "target"}
BOOLEAN_PAYLOAD_KEYS = {"state"}
URI_PAYLOAD_KEYS = {"zone"}


def prop_uri(name: str) -> URIRef:
    return URIRef(f"{URN_PROP_NS}{name}")


def parse_timestamp(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _resolve_project_path(path_like: str) -> Path:
    p = Path(path_like)
    if p.is_absolute():
        return p
    cwd_candidate = Path.cwd() / p
    if cwd_candidate.exists():
        return cwd_candidate
    return Path(__file__).resolve().parent.parent / p


def load_state(path: str) -> Graph:
    graph = Graph()
    resolved = _resolve_project_path(path)
    text = resolved.read_text(encoding="utf-8")
    graph.parse(data=text, format="turtle")
    return graph


def load_events(path: str) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    resolved = _resolve_project_path(path)
    with open(resolved, "r", encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if stripped:
                events.append(json.loads(stripped))
    return events


def _event_order_key(event: Dict[str, Any], ordering: List[str]) -> Tuple[Any, ...]:
    key: List[Any] = []
    for field in ordering:
        if field == "timestamp":
            key.append(parse_timestamp(str(event[field])))
        else:
            key.append(event.get(field))
    return tuple(key)


def sort_events(events: List[Dict[str, Any]], ordering: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    ordering = ordering or ["timestamp", "eid"]
    return sorted(events, key=lambda event: _event_order_key(event, ordering))


def _to_literal_for_payload(key: str, value: Any) -> Literal | URIRef:
    if key in URI_PAYLOAD_KEYS:
        return URIRef(str(value))

    if key in BOOLEAN_PAYLOAD_KEYS:
        if isinstance(value, str):
            coerced = value.strip().lower() == "true"
        else:
            coerced = bool(value)
        return Literal(coerced, datatype=XSD.boolean)

    if key in NUMERIC_PAYLOAD_KEYS:
        return Literal(float(value), datatype=XSD.decimal)

    if isinstance(value, bool):
        return Literal(value, datatype=XSD.boolean)
    if isinstance(value, int):
        return Literal(value)
    if isinstance(value, float):
        return Literal(value, datatype=XSD.decimal)

    return Literal(str(value))


def _add_event_to_graph(graph: Graph, event: Dict[str, Any], ordinal: int) -> None:
    event_uri = URIRef(f"urn:event:{event['eid']}")
    payload = event.get("payload", {})

    graph.add((event_uri, prop_uri("eid"), Literal(str(event["eid"]))))
    graph.add((event_uri, prop_uri("timestamp"), Literal(str(event["timestamp"]))))
    graph.add((event_uri, prop_uri("type"), Literal(str(event["type"]))))
    graph.add((event_uri, prop_uri("role"), Literal(str(event["role"]))))
    graph.add((event_uri, prop_uri("order"), Literal(ordinal)))

    for key, value in payload.items():
        graph.add((event_uri, prop_uri(key), _to_literal_for_payload(key, value)))


def _sliding_window_bounds(anchor_dt: datetime, length_minutes: int) -> Tuple[datetime, datetime]:
    return anchor_dt - timedelta(minutes=length_minutes), anchor_dt


def _tumbling_window_bounds(anchor_dt: datetime, length_minutes: int, step_minutes: int) -> Tuple[datetime, datetime]:
    step_seconds = max(step_minutes, 1) * 60
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    anchor_seconds = int((anchor_dt - epoch).total_seconds())
    bucket_index = anchor_seconds // step_seconds
    window_end_dt = epoch + timedelta(seconds=(bucket_index + 1) * step_seconds)
    window_start_dt = window_end_dt - timedelta(minutes=length_minutes)
    return window_start_dt, window_end_dt


def select_window_events(
    events: List[Dict[str, Any]],
    settings: Optional[Dict[str, Any]] = None,
    anchor_timestamp: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    settings = settings or {}
    ordered = sort_events(events, settings.get("event_ordering", ["timestamp", "eid"]))

    if not ordered:
        meta = {
            "window_type": settings.get("window", {}).get("type", "sliding"),
            "window_id": "empty",
            "window_start": None,
            "window_end": None,
            "anchor_timestamp": anchor_timestamp,
            "selected_event_ids": [],
        }
        return [], meta

    window_cfg = settings.get("window", {})
    window_type = window_cfg.get("type", "sliding")
    length_minutes = int(window_cfg.get("length_minutes", 5))
    step_minutes = int(window_cfg.get("step_minutes", length_minutes))

    if anchor_timestamp is None:
        anchor_dt = max(parse_timestamp(str(event["timestamp"])) for event in ordered)
        effective_anchor = anchor_dt.isoformat()
    else:
        anchor_dt = parse_timestamp(anchor_timestamp)
        effective_anchor = anchor_timestamp

    if window_type == "sliding":
        window_start_dt, window_end_dt = _sliding_window_bounds(anchor_dt, length_minutes)
        selected = [
            event
            for event in ordered
            if window_start_dt <= parse_timestamp(str(event["timestamp"])) <= window_end_dt
        ]
    elif window_type == "tumbling":
        window_start_dt, window_end_dt = _tumbling_window_bounds(anchor_dt, length_minutes, step_minutes)
        selected = [
            event
            for event in ordered
            if window_start_dt <= parse_timestamp(str(event["timestamp"])) < window_end_dt
        ]
    else:
        selected = ordered
        window_start_dt = parse_timestamp(str(selected[0]["timestamp"]))
        window_end_dt = parse_timestamp(str(selected[-1]["timestamp"]))

    window_id = (
        f"{window_type}:{window_start_dt.isoformat()}::{window_end_dt.isoformat()}"
        if selected
        else "empty"
    )

    meta = {
        "window_type": window_type,
        "window_id": window_id,
        "window_start": window_start_dt.isoformat(),
        "window_end": window_end_dt.isoformat(),
        "anchor_timestamp": effective_anchor,
        "selected_event_ids": [event["eid"] for event in selected],
    }
    return selected, meta


def build_dataset(
    state_graph: Graph,
    events: List[Dict[str, Any]],
    settings: Optional[Dict[str, Any]] = None,
    anchor_timestamp: Optional[str] = None,
) -> Tuple[Dataset, Dict[str, Any]]:
    settings = settings or {}
    dataset = Dataset()

    state = dataset.graph(STATE_GRAPH_IRI)
    for triple in state_graph:
        state.add(triple)

    selected_events, window_meta = select_window_events(events, settings=settings, anchor_timestamp=anchor_timestamp)

    alias_graph = dataset.graph(WINDOW_ALIAS_IRI)
    named_window_graph = dataset.graph(URIRef(f"urn:window:{window_meta['window_id']}"))

    for ordinal, event in enumerate(selected_events):
        _add_event_to_graph(alias_graph, event, ordinal)
        _add_event_to_graph(named_window_graph, event, ordinal)

    window_meta["selected_events"] = selected_events
    return dataset, window_meta


if __name__ == "__main__":
    state_graph = load_state("shapes/base_graph.ttl")
    events = load_events("data/events.jsonl")
    dataset, meta = build_dataset(
        state_graph,
        events,
        settings={
            "window": {"type": "sliding", "length_minutes": 5, "step_minutes": 1},
            "event_ordering": ["timestamp", "eid"],
        },
    )

    print("Window metadata:")
    print(json.dumps(meta, indent=2))
    print("Named graphs in dataset:")
    for graph in dataset.graphs():
        print(graph.identifier)