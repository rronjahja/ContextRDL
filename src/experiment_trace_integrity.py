"""
Trace integrity experiment.

Two checks:
  (1) Replay the recorded ``results/trace.json`` using the full-execution
      replayer (``replay_full.replay_full``).  This verifies that every layer
      of the pipeline (rule evaluation, scheduling, resolution) reproduces
      the recorded execution, not just the final state.

  (2) Fresh round-trip: re-run the default workload, save a new trace, then
      replay that.  This verifies that trace generation and replay are
      mutually consistent for any newly produced execution.

Writes ``results/experiment_trace_integrity.json``.
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict

from engine import run_engine
from replay_full import replay_full


def run_recorded_trace_check(trace_path: str = "results/trace.json") -> Dict[str, Any]:
    if not os.path.exists(trace_path):
        return {
            "trace_path":    trace_path,
            "skipped":       True,
            "skip_reason":   f"{trace_path} not found; run engine.py first",
        }
    report = replay_full(trace_path=trace_path)
    return {"trace_path": trace_path, "report": report, "skipped": False}


def run_fresh_round_trip() -> Dict[str, Any]:
    """Produce a fresh trace via run_engine and replay it."""
    fresh_path = "results/trace_fresh_for_integrity.json"
    run_engine(
        trace_path=fresh_path,
        save_trace_file=True,
    )
    report = replay_full(trace_path=fresh_path)
    return {"trace_path": fresh_path, "report": report, "skipped": False}


def main():
    recorded = run_recorded_trace_check("results/trace.json")
    fresh    = run_fresh_round_trip()

    out = {
        "recorded": recorded,
        "fresh_round_trip": fresh,
        "overall_pass": (
            (recorded.get("skipped") or recorded["report"]["overall_pass"])
            and fresh["report"]["overall_pass"]
        ),
    }
    os.makedirs("results", exist_ok=True)
    with open("results/experiment_trace_integrity.json", "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2)

    def _summary(rec):
        if rec.get("skipped"):
            return f"SKIPPED ({rec['skip_reason']})"
        r = rec["report"]
        flags = [
            ("enabled",    r["enabled_match"]),
            ("schedule",   r["schedule_match"]),
            ("accepted",   r["accepted_match"]),
            ("decisions",  r["decisions_match"]),
            ("digest",     r["digest_match"]),
        ]
        return " ".join(f"{k}={'OK' if v else 'FAIL'}" for k, v in flags)

    print("Recorded trace replay:       ", _summary(recorded))
    print("Fresh round-trip replay:     ", _summary(fresh))
    print("\nWrote results/experiment_trace_integrity.json")


if __name__ == "__main__":
    main()
