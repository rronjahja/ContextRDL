"""
Role filter (gate (i) of Definition 9) exercised with an inactive role.

In every headline workload all issuing roles are active, so gate (i) never
rejects. This experiment runs the default workload under two role contexts
from data/contexts.json in which one role is inactive:

  occupant-inactive : r1 and r4 (occupant) are constructed, then rejected by
                      the role filter with reason ``inactive_role``. The
                      accepted set (and hence the successor digest) is the
                      same as in the default context, because r1 was rejected
                      by the policy guard and r4 was shadowed there anyway;
                      the traces differ only in the recorded reason codes.
  operator-inactive : r2, r3, r7, r8 (operator) are rejected by the role
                      filter; the committed state changes accordingly.

Each produced trace is replayed with replay_full, which must reproduce the
``inactive_role`` decisions from the recorded role context.

Run from the project root:
    python src/experiment_role_filter.py
Writes results/hvac/experiment_role_filter.json and results/hvac/traces/trace_role_<context>.json
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if (HERE / "rule_engine.py").exists():
    sys.path.insert(0, str(HERE))
    PROJECT_ROOT = HERE.parent
else:
    PROJECT_ROOT = HERE
import paths  # noqa: E402  (results layout)
os.chdir(PROJECT_ROOT)

from engine import run_engine  # noqa: E402
from replay_full import replay_full  # noqa: E402


def run_context(context_name: str):
    trace_path = paths.hvac_trace(f"trace_role_{context_name}.json")
    _, schedule, accepted, _, trace = run_engine(context_name=context_name, trace_path=trace_path)
    replay = replay_full(trace_path=trace_path)
    return {
        "context": context_name,
        "active_roles": trace["window"]["governance_context"]["active_roles"],
        "enabled": len(schedule),
        "accepted_rids": [a["rid"] for a in accepted],
        "decisions": [{"rid": d["rid"], "role": d["role"], "accepted": d["accepted"],
                       "reason": d["reason"].split(":")[0]} for d in trace["decisions"]],
        "successor_digest": trace["successor_graph"]["digest"],
        "replay_overall_pass": replay["overall_pass"],
        "trace_path": trace_path,
    }


def main():
    paths.ensure_dirs()
    out = {name: run_context(name) for name in ("default", "occupant-inactive", "operator-inactive")}
    out["occupant_inactive_same_successor_as_default"] = (
        out["default"]["successor_digest"] == out["occupant-inactive"]["successor_digest"])
    for name in ("default", "occupant-inactive", "operator-inactive"):
        r = out[name]
        print(f"== context {name}: active={r['active_roles']} accepted={r['accepted_rids']} "
              f"digest={r['successor_digest'][:12]} replay={'PASS' if r['replay_overall_pass'] else 'FAIL'}")
        for d in r["decisions"]:
            print(f"   {d['rid']:3s} {d['role']:9s} {'accept' if d['accepted'] else 'reject'}  {d['reason']}")
    with open(paths.hvac("experiment_role_filter.json"), "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2)
    print("Wrote", paths.relative(paths.hvac("experiment_role_filter.json")))
    if not all(out[n]["replay_overall_pass"] for n in ("default", "occupant-inactive", "operator-inactive")):
        raise SystemExit("replay mismatch")


if __name__ == "__main__":
    main()
