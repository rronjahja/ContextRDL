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
                 delivery mode is recorded in the trace settings and the
                 ledger in the window metadata of every step trace
                 (results/hvac/traces/trace_multiwindow_<mode>_step<k>.json).

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
Writes results/hvac/experiment_multiwindow.json
"""
from __future__ import annotations
import paths  # noqa: E402  (results layout)

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Set

from rdflib import URIRef

from engine import run_engine
from dataset_builder import select_window_events
from rule_engine import load_settings

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


OCCUPANCY_OCCURRENCE = {"evt-occB-1", "evt-occB-1-dup"}  # one physical occurrence, two log identifiers


def r4_firings(trace: Dict[str, Any], eid) -> int:
    """How often the given event identifier(s) produced an ACCEPTED r4 action.
    Passing the set OCCUPANCY_OCCURRENCE counts the physical occurrence
    regardless of which of its two identifiers won the same-target conflict."""
    eids = {eid} if isinstance(eid, str) else set(eid)
    accepted_aids = {d["aid"] for d in trace["decisions"] if d.get("accepted")}
    n = 0
    for action in trace.get("schedule", []):
        if action.get("rid") == "r4" and action.get("aid") in accepted_aids \
                and str(action.get("event_id", "")).split("#")[-1] in eids:
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
        # The delivery mode is a component of the execution configuration
        # (Definition 11 (ii)) and the processed-event ledger is part of the
        # recorded trace (Section III-G): both are handed to run_engine so that
        # they land in the trace settings and window metadata of every step.
        # Events selected into this window are known before the step runs
        # (event-time windowing over the visible log), so both the ledger
        # before and the ledger after the step can be recorded in the trace.
        _, meta_preview = select_window_events(visible, load_settings(), anchor)
        selected_ids = {str(eid) for eid in meta_preview.get("selected_event_ids", [])}
        ledger_after = (consumed | selected_ids) if mode == "consume_once" else set()
        _, schedule, accepted, graph, trace = run_engine(
            state_graph=graph,
            events=visible,
            anchor_timestamp=anchor,
            save_trace_file=True,
            trace_path=paths.hvac_trace(f"trace_multiwindow_{mode}_step{i + 1}.json"),
            settings_override={"event_delivery": mode},
            extra_window_meta={
                "processed_event_ledger_before_step": sorted(consumed),
                "processed_event_ledger_after_step": sorted(ledger_after),
                "log_event_ids_visible": [e["eid"] for e in visible],
            },
        )
        window_eids = {str(e["eid"]) for e in trace.get("events", [])}
        assert window_eids == selected_ids, "window selection preview disagrees with the engine"
        if mode == "consume_once":
            # Exact identifier membership: a ledger entry "future" must not
            # consume a distinct event "future-suffix", and vice versa.
            consumed |= {str(e["eid"]) for e in visible if str(e["eid"]) in window_eids}
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
            "r4_firings_from_occurrence": r4_firings(trace, OCCUPANCY_OCCURRENCE),
            "accepted_r4_event_ids": [str(a.get("event_id")).split("#")[-1] for a in accepted if a["rid"] == "r4"],
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
        total_r4 = sum(s["r4_firings_from_occurrence"] for s in example["steps"])
        for s in example["steps"]:
            print(f"  {s['anchor']}  enabled={s['enabled']} accepted={s['accepted']} "
                  f"{s['accepted_rids']}  ZoneB={s['zoneB_setpoint']}  late_dropped={s['late_event_dropped']}")
        print(f"  -> the occupancy occurrence (identifiers {sorted(OCCUPANCY_OCCURRENCE)}) fired r4 {total_r4} time(s) across the run")
        results[mode]["occurrence_r4_total_firings"] = total_r4
        results[mode]["occB1_r4_total_firings"] = sum(s["r4_firings_from_occB1"] for s in example["steps"])

    out = Path(paths.hvac("experiment_multiwindow.json"))
    out.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    print("\nWrote", out)
    seqs = {m: [str(s["zoneB_setpoint"]) for s in results[m]["steps"]] for m in results}
    if not (results["redelivery"]["unique_digest_sequences"] == 1 and results["consume_once"]["unique_digest_sequences"] == 1
            and [float(x) for x in seqs["redelivery"]] == [21.0, 22.0, 23.0]
            and [float(x) for x in seqs["consume_once"]] == [21.0, 21.0, 23.0]):
        raise SystemExit("multi-window outcome differs from the characterized behaviour")
    return results


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 30)
