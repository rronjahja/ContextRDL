"""
Restart-safe evaluation steps over a StepStore.

One committed step = one atomic operation over (successor graph, full trace
including the processed-event ledger after the step, CURRENT pointer).
On every call the controller resumes from the store: the current state graph
and the ledger of the current step. Under consume-once delivery, events whose
identifiers are on the ledger are excluded by exact membership; every event
identifier visible in the window is added to the ledger of the new step, so a
crash before the pointer switch leaves the ledger unchanged (the event is
delivered again, once), and a retry after the pointer switch finds the event
on the ledger (it is not delivered again). This yields at most one committed
evaluation step per event identifier across restarts; a committed step may
accept zero, one, or several actions, within the store's single-writer
assumption. Late events (never covered by a later window) are dropped, as in
the in-memory harness.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from rdflib import Graph

from engine import run_engine
from persistence import StepStore, graph_digest


def run_committed_step(
    store: StepStore,
    event_log: List[Dict[str, Any]],
    anchor_timestamp: str,
    initial_graph: Optional[Graph] = None,
    delivery_mode: str = "consume_once",
    crash_after: Optional[str] = None,
    settings_override: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    state = store.current_state()
    if state is None:
        if initial_graph is None:
            raise ValueError("empty store and no initial graph")
        state = initial_graph
    ledger_before = set(store.current_ledger()) if delivery_mode == "consume_once" else set()
    candidates = [e for e in event_log if str(e["eid"]) not in ledger_before]

    override = dict(settings_override or {})
    override["event_delivery"] = delivery_mode
    graph_t, schedule, accepted, successor, trace = run_engine(
        state_graph=state, events=candidates, anchor_timestamp=anchor_timestamp,
        save_trace_file=False, settings_override=override,
        extra_window_meta={"processed_event_ledger_before_step": sorted(ledger_before)},
    )
    visible_ids = {str(e["eid"]) for e in trace.get("events", [])}
    ledger_after = ledger_before | visible_ids if delivery_mode == "consume_once" else set()
    step_id = store.commit_step(graph_digest(graph_t), trace["window"]["window_id"], successor, dict(trace),
                                crash_after=crash_after, ledger_after=sorted(ledger_after))
    return {"step_id": step_id, "window_id": trace["window"]["window_id"],
            "visible_event_ids": sorted(visible_ids), "accepted_rids": [a["rid"] for a in accepted],
            "ledger_after": sorted(ledger_after), "successor_digest": trace["successor_graph"]["digest"]}
