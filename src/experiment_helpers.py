from __future__ import annotations

from typing import Dict, List

from rdflib import URIRef


ZONE_B = "http://example.org/building#ZoneB"
CURRENT_SETPOINT = URIRef("http://example.org/building#currentSetpoint")


def tie_conflict_events() -> List[Dict[str, object]]:
    timestamp = "2026-03-14T09:00:00Z"
    return [
        {
            "eid": "tie-demand-001",
            "timestamp": timestamp,
            "type": "DemandResponsePeak",
            "role": "operator",
            "payload": {
                "zone": ZONE_B,
                "cap": 21,
            },
        },
        {
            "eid": "tie-preheat-001",
            "timestamp": timestamp,
            "type": "PreheatRequest",
            "role": "operator",
            "payload": {
                "zone": ZONE_B,
                "target": 22,
            },
        },
    ]


def governance_conflict_events() -> List[Dict[str, object]]:
    timestamp = "2026-03-14T09:00:00Z"
    return [
        {
            "eid": "gov-occupant-001",
            "timestamp": timestamp,
            "type": "OccupantSetpointRequest",
            "role": "occupant",
            "payload": {
                "zone": ZONE_B,
                "delta": 2,
            },
        },
        {
            "eid": "gov-operator-001",
            "timestamp": timestamp,
            "type": "DemandResponsePeak",
            "role": "operator",
            "payload": {
                "zone": ZONE_B,
                "cap": 21,
            },
        },
    ]


def duplicate_identity_events() -> List[Dict[str, object]]:
    timestamp = "2026-03-14T09:00:00Z"
    return [
        {
            "eid": "dup-001",
            "timestamp": timestamp,
            "type": "OccupantSetpointRequest",
            "role": "occupant",
            "payload": {
                "zone": ZONE_B,
                "delta": 2,
            },
        },
        {
            "eid": "dup-002",
            "timestamp": timestamp,
            "type": "OccupantSetpointRequest",
            "role": "occupant",
            "payload": {
                "zone": ZONE_B,
                "delta": 2,
            },
        },
    ]
