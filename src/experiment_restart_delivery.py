"""
Restart-safe consume-once delivery through the persistence store.

Uses run_controller.run_committed_step, which resumes from CURRENT (state and
processed-event ledger) on every call. Four scenarios over an event log with
one occupancy event for ZoneB (increment rule r4) and a duplicate identifier:

  commit_then_retry      : step committed, process "restarts" (new StepStore
                           object on the same directory), the same window is
                           evaluated again: the event is on the ledger, so the
                           window is empty, no action fires and an empty step
                           commits (a re-execution of the same G_t and window
                           would instead be refused as a duplicate).
  crash_then_retry       : crash before the pointer switch, restart, recover,
                           evaluate again: the event is delivered once.
  later_window           : a later anchor whose sliding window still covers
                           the event: not delivered again under consume-once.
  redelivery_reference   : same later window under redelivery: delivered again
                           (increment applied twice), for contrast.

Run from the project root:
    python src/experiment_restart_delivery.py
Writes results/hvac/experiment_restart_delivery.json
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
if (HERE / "rule_engine.py").exists():
    sys.path.insert(0, str(HERE))
    PROJECT_ROOT = HERE.parent
else:
    PROJECT_ROOT = HERE
import paths  # noqa: E402  (results layout)
os.chdir(PROJECT_ROOT)

from rdflib import URIRef  # noqa: E402

from dataset_builder import load_state  # noqa: E402
from persistence import CrashPoint, StepStore  # noqa: E402
from run_controller import run_committed_step  # noqa: E402

ZONE_B = URIRef("http://example.org/building#ZoneB")
SETPOINT = URIRef("http://example.org/building#currentSetpoint")
LOG = [
    {"eid": "evt-occB-1", "timestamp": "2026-03-14T09:00:00Z", "type": "OccupancyDetected", "role": "occupant",
     "payload": {"zone": str(ZONE_B)}},
    {"eid": "evt-occB-1-dup", "timestamp": "2026-03-14T09:00:00Z", "type": "OccupancyDetected", "role": "occupant",
     "payload": {"zone": str(ZONE_B)}},
]
A1, A2 = "2026-03-14T09:04:00Z", "2026-03-14T09:05:00Z"


def zone_b(store: StepStore):
    g = store.current_state()
    return None if g is None else float(next(g.objects(ZONE_B, SETPOINT)).toPython())


def main():
    root = Path(tempfile.mkdtemp(prefix="restart_"))
    results = {}
    try:
        base = load_state("data/base_graph.ttl")

        # 1) commit, restart, retry the same window
        store = StepStore(root / "retry")
        first = run_committed_step(store, LOG, A1, initial_graph=base)
        store = StepStore(root / "retry")  # restart
        retry = run_committed_step(store, LOG, A1, initial_graph=base)
        results["commit_then_retry"] = {"first_accepted": first["accepted_rids"], "ledger_after": first["ledger_after"],
                                        "retry_visible_events": retry["visible_event_ids"],
                                        "retry_accepted": retry["accepted_rids"],
                                        "retry_committed_empty_step": retry["step_id"] != first["step_id"],
                                        "zone_b": zone_b(store),
                                        "invariant_holds": first["accepted_rids"] == ["r4"] and retry["accepted_rids"] == []
                                        and retry["visible_event_ids"] == [] and zone_b(store) == 21.0}

        # 2) crash before the pointer switch, restart, recover, retry
        store = StepStore(root / "crash")
        try:
            run_committed_step(store, LOG, A1, initial_graph=base, crash_after="state")
        except CrashPoint:
            pass
        store = StepStore(root / "crash")  # restart
        recovery = store.recover()
        second = run_committed_step(store, LOG, A1, initial_graph=base)
        results["crash_then_retry"] = {"discarded": recovery["discarded_incomplete_steps"], "accepted": second["accepted_rids"],
                                       "ledger_after": second["ledger_after"], "zone_b": zone_b(store),
                                       "invariant_holds": len(recovery["discarded_incomplete_steps"]) == 1
                                       and second["accepted_rids"] == ["r4"] and zone_b(store) == 21.0}

        # 3) later window still covering the event: consume-once vs redelivery
        for mode in ("consume_once", "redelivery"):
            store = StepStore(root / mode)
            run_committed_step(store, LOG, A1, initial_graph=base, delivery_mode=mode)
            store = StepStore(root / mode)  # restart
            later = run_committed_step(store, LOG, A2, delivery_mode=mode)
            results[f"later_window_{mode}"] = {"accepted": later["accepted_rids"], "visible": later["visible_event_ids"],
                                               "zone_b": zone_b(store), "chain": store.committed_chain()}
        results["later_window_consume_once"]["invariant_holds"] = (
            results["later_window_consume_once"]["accepted"] == [] and results["later_window_consume_once"]["zone_b"] == 21.0)
        results["later_window_redelivery"]["invariant_holds"] = (
            results["later_window_redelivery"]["accepted"] == ["r4"] and results["later_window_redelivery"]["zone_b"] == 22.0)
    finally:
        shutil.rmtree(root, ignore_errors=True)

    for name, res in results.items():
        print(f"{name:<28} -> {json.dumps(res, default=str)[:150]}")
    out = Path(paths.hvac("experiment_restart_delivery.json"))
    out.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    print("Wrote", out)
    if not all(r["invariant_holds"] for r in results.values()):
        raise SystemExit("restart delivery invariant violated")


if __name__ == "__main__":
    main()
