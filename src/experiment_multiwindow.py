"""
Multi-step execution over an evolving graph (reviewer concern R1-3).

The single-step semantics is composed into a run: the committed successor
G_{t+1} of step t is the authoritative input graph of step t+1, and the
windowing policy is applied at a fixed sequence of anchor instants over the
same append-only event log. Because the 5-minute sliding window advances by
1 minute, the same physical event is visible in up to five consecutive
windows. Two documented event-delivery modes are compared:

  redelivery   : the step input is the raw window content (the previous
                 behaviour). An event visible in k windows enables its rule
                 k times, each with a distinct window identifier; for
                 increment-style rules (r4) the increment is applied
                 repeatedly.
  consume_once : the harness carries a processed-event ledger across
                 steps. Every eid contained in a step's input window is
                 marked processed after the step commits; processed events
                 are excluded from later step inputs, giving at-most-once
                 delivery of each physical event to an evaluation step. The
                 ledger is recorded per step in the results and would be
                 recorded in the trace in a deployment.

The experiment also injects (a) an exact duplicate event (same type,
timestamp, payload, distinct eid: an upstream retransmission), handled
within one step by action-instance identity plus the conflict gate, and
(b) a late event that arrives after the windows covering its timestamp
have been evaluated; under event-time windowing it is never inside a later
window and is dropped (and reported). Determinism of the full three-step
run is checked over `runs` repetitions per mode. The headline quantity is
how many times the SAME physical occupancy event fires rule r4 across the
run: twice under redelivery (its increment is applied in two overlapping
windows), once under consume_once.

Usage:
    python experiment_multiwindow.py [runs]
Writes results/experiment_multiwindow.json
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Set

from rdflib import URIRef

from engine import run_engine

EX = "http://example.org/building#"
ZONE_B = URIRef(f"{EX}ZoneB")
CURRENT_SETPOINT = URIRef(f"{EX}currentSetpoint")

ANCHORS = ["2026-03-14T09:04:00Z", "2026-03-14T09:06:00Z", "2026-03-14T09:08:00Z"]


def event_log() -> List[Dict[str, Any]]:
    """Append-only log spanning the three windows, with a duplicate and a
    late event."""
    return [
        # occupancy at 09:01:30 -> inside windows anchored 09:04 and 09:06
        # (5-minute sliding window); r4 increments ZoneB setpoint by 1.
        {"eid": "evt-occB-1", "type": "OccupancyDetected", "timestamp": "2026-03-14T09:01:30Z",
         "role": "occupant", "payload": {"zone": f"{EX}ZoneB"}},
        # exact duplicate: same type/timestamp/payload, distinct eid
        # (upstream retransmission).
        {"eid": "evt-occB-1-dup", "type": "OccupancyDetected", "timestamp": "2026-03-14T09:01:30Z",
         "role": "occupant", "payload": {"zone": f"{EX}ZoneB"}},
        # operator preheat at 09:07 -> only the window anchored 09:08.
        {"eid": "evt-preB-1", "type": "PreheatRequest", "timestamp": "2026-03-14T09:07:10Z",
         "role": "operator", "payload": {"zone": f"{EX}ZoneB", "target": 23}},
        # LATE event: timestamp 09:02 but appended to the log only after the
        # 09:04 and 09:06 steps have run; the 09:08 window [09:03, 09:08]
        # no longer covers it, so event-time windowing drops it.
        {"eid": "evt-late-1", "type": "OccupancyDetected", "timestamp": "2026-03-14T09:02:00Z",
         "role": "occupant", "payload": {"zone": f"{EX}ZoneB"}},
    ]


def arrival_visible(log: List[Dict[str, Any]], step_index: int) -> List[Dict[str, Any]]:
    """Model processing-time arrival: the late event enters the log only
    before the third step."""
    if step_index < 2:
        return [e for e in log if e["eid"] != "evt-late-1"]
    return log


def r4_firings(trace: Dict[str, Any], eid: str) -> int:
    """How often the given physical event produced an ACCEPTED r4 action."""
    accepted_aids = {d["aid"] for d in trace["decisions"] if d.get("accepted")}
    n = 0
    for action in trace.get("schedule", []):
        if action.get("rid") == "r4" and action.get("aid") in accepted_aids \
                and str(action.get("event_id", "")).endswith(eid):
            n += 1
    return n


def run_sequence(mode: str) -> Dict[str, Any]:
    assert mode in ("redelivery", "consume_once")
    log = event_log()
    consumed: Set[str] = set()
    graph = None
    steps = []
    for i, anchor in enumerate(ANCHORS):
        visible = arrival_visible(log, i)
        if mode == "consume_once":
            visible = [e for e in visible if e["eid"] not in consumed]
        _, schedule, accepted, graph, trace = run_engine(
            state_graph=graph,
            events=visible,
            anchor_timestamp=anchor,
            save_trace_file=False,
        )
        window_eids = {str(e["eid"]) for e in trace.get("events", [])} if trace.get("events") else set()
        if not window_eids:
            # fall back: eids referenced by scheduled actions
            window_eids = {str(a["event_id"]).split("#")[-1] for a in trace.get("schedule", []) if a.get("event_id")}
        if mode == "consume_once":
            consumed |= {e["eid"] for e in visible
                         if any(str(e["eid"]) in w for w in window_eids) or e["eid"] in window_eids}
        zone_b = None
        for obj in graph.objects(ZONE_B, CURRENT_SETPOINT):
            zone_b = obj.toPython()
        dropped_late = [e["eid"] for e in visible if e["eid"] == "evt-late-1"
                        and not any("evt-late-1" in str(a.get("event_id", "")) for a in schedule)]
        steps.append({
            "anchor": anchor,
            "window_id": trace["window"]["window_id"],
            "enabled": len(schedule),
            "accepted": len(accepted),
            "accepted_rids": [a["rid"] for a in accepted],
            "r4_firings_from_occB1": r4_firings(trace, "evt-occB-1"),
            "late_event_dropped": dropped_late,
            "zoneB_setpoint": zone_b,
            "successor_digest": trace["successor_graph"]["digest"],
            "consumed_ledger": sorted(consumed) if mode == "consume_once" else None,
        })
    return {"mode": mode, "steps": steps,
            "digest_sequence": tuple(s["successor_digest"] for s in steps)}


def main(runs: int = 30):
    results: Dict[str, Any] = {}
    for mode in ("redelivery", "consume_once"):
        sequences = {run_sequence(mode)["digest_sequence"] for _ in range(runs)}
        example = run_sequence(mode)
        results[mode] = {
            "runs": runs,
            "unique_digest_sequences": len(sequences),
            "steps": example["steps"],
        }
        print(f"\n=== mode: {mode} (runs={runs}, unique sequences={len(sequences)}) ===")
        total_r4 = sum(s["r4_firings_from_occB1"] for s in example["steps"])
        for s in example["steps"]:
            print(f"  {s['anchor']}  enabled={s['enabled']} accepted={s['accepted']} "
                  f"{s['accepted_rids']}  ZoneB={s['zoneB_setpoint']}  late_dropped={s['late_event_dropped']}")
        print(f"  -> physical event evt-occB-1 fired r4 {total_r4} time(s) across the run")
        results[mode]["occB1_r4_total_firings"] = total_r4

    out = Path(__file__).resolve().parent.parent / "results" / "experiment_multiwindow.json"
    out.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    print("\nWrote", out)
    return results


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 30)
