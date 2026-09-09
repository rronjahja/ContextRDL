"""
Replay verification for the replay-table rows not produced by
experiment_hvac_v3.py: the determinism-stress workload at N=64, the
governance-clean workload of Table 9 under both precedence configurations,
and the four EV-charging windows.

Both rows are trace-based replays (level 2 of Section V-K), not repeated
in-process execution: the recording run writes a trace file that contains the
input graph snapshot (canonical N-Triples), the constructed action instances,
the execution configuration, the schedule, every decision with its reason
code, the accepted set and the successor digest. The replay run reads that
file, rebuilds the input graph from the snapshot, re-schedules the recorded
action instances, re-runs the resolver, and compares the regenerated
schedule, every decision record (all fields except the validator's free-text
report), accepted set and successor digest against the recorded values.

The stress workload is built from synthetic action instances (there is no
rule file to re-evaluate), so its replay starts at scheduling. The EV replay
is delegated to ev/experiment_ev.py, which records and replays its own traces
under the same protocol.

Run from the project root:
    python src/experiment_replay_extra.py
Writes results/hvac/traces/trace_stress_64.json, results/hvac/traces/trace_governance_clean_*.json
and results/hvac/experiment_replay_extra.json
"""
from __future__ import annotations

import copy
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
os.chdir(PROJECT_ROOT)

from rdflib import Graph  # noqa: E402

from resolver import resolve_actions as resolve_original  # noqa: E402
from rule_engine import schedule_actions  # noqa: E402
from trace import file_sha256, graph_digest, graph_from_snapshot, serialize_graph_snapshot  # noqa: E402
from rule_engine import BINDING_PROFILE  # noqa: E402

from experiment_determinism_stress import (  # noqa: E402
    _synth_graph, _synth_actions, _settings,
)
from experiment_governance_v2 import _build_actions as _gov_actions  # noqa: E402


def _decision_keys(decisions):
    """All recorded decision fields except the free-text validator report."""
    return [json.loads(json.dumps({k: v for k, v in d.items() if k != "validation_report"},
                                  sort_keys=True, default=str)) for d in decisions]


def _record(workload: str, base: Graph, actions: list, settings: dict, trace_path: str) -> dict:
    schedule = schedule_actions(copy.deepcopy(actions), settings=settings)
    accepted, successor, decisions = resolve_original(
        base, schedule, shapes_path="shapes/invariants.ttl", settings=settings)
    settings = dict(settings)
    settings["dependencies"] = {"shapes_path": "shapes/invariants.ttl",
                                "shapes_sha256": file_sha256("shapes/invariants.ttl"),
                                "binding_profile": BINDING_PROFILE,
                                "admissibility_regime": os.environ.get("ADMISSIBILITY_REGIME", "incremental").lower()}
    trace = {
        "workload": workload,
        "settings": settings,
        "input_graph": serialize_graph_snapshot(base),
        "actions": actions,
        "schedule_aids": [a["aid"] for a in schedule],
        "decisions": _decision_keys(decisions),
        "accepted_aids": [a["aid"] for a in accepted],
        "successor_digest": graph_digest(successor),
    }
    with open(trace_path, "w", encoding="utf-8") as fh:
        json.dump(trace, fh, indent=2, default=str)
    return trace


def record_stress(n: int, trace_path: str) -> dict:
    return _record(f"stress N={n}", _synth_graph(), _synth_actions(n), _settings(), trace_path)


def record_governance_clean(label: str, role_rank_map: dict, trace_path: str) -> dict:
    """The governance-clean workload of Table 9 (synthetic actions A_unsafe,
    A_op, A_occ on the default base graph) under a given role precedence."""
    base = Graph()
    base.parse("data/base_graph.ttl", format="turtle")
    settings = _settings()
    settings["role_precedence"] = dict(role_rank_map)
    return _record(f"governance-clean ({label})", base, _gov_actions(role_rank_map), settings, trace_path)


def replay_stress(trace_path: str) -> dict:
    with open(trace_path, "r", encoding="utf-8") as fh:
        trace = json.load(fh)
    base = graph_from_snapshot(trace["input_graph"])
    deps = trace["settings"].get("dependencies", {})
    dependency_ok = (graph_digest(base) == trace["input_graph"]["digest"]
                     and file_sha256(deps.get("shapes_path", "shapes/invariants.ttl")) == deps.get("shapes_sha256")
                     and deps.get("binding_profile") == BINDING_PROFILE
                     and all(a.get("binding_profile", BINDING_PROFILE) == BINDING_PROFILE for a in trace["actions"]))
    previous = os.environ.get("ADMISSIBILITY_REGIME")
    os.environ["ADMISSIBILITY_REGIME"] = deps.get("admissibility_regime", "incremental")
    try:
        schedule = schedule_actions(copy.deepcopy(trace["actions"]), settings=trace["settings"])
        accepted, successor, decisions = resolve_original(
            base, schedule, shapes_path=deps.get("shapes_path", "shapes/invariants.ttl"), settings=trace["settings"])
    finally:
        if previous is None:
            os.environ.pop("ADMISSIBILITY_REGIME", None)
        else:
            os.environ["ADMISSIBILITY_REGIME"] = previous
    regen_decisions = _decision_keys(decisions)
    rec_decisions = _decision_keys(trace["decisions"])
    return {
        "workload": trace["workload"],
        "n": len(trace["actions"]),
        "trace_path": trace_path,
        "dependencies_match": dependency_ok,
        "enabled_recorded": len(trace["actions"]),
        "enabled_replayed": len(schedule),
        "schedule_match": [a["aid"] for a in schedule] == trace["schedule_aids"],
        "decisions_match": regen_decisions == rec_decisions,
        "decisions_compared": len(rec_decisions),
        "accepted_match": [a["aid"] for a in accepted] == trace["accepted_aids"],
        "digest_match": graph_digest(successor) == trace["successor_digest"],
        "successor_digest": trace["successor_digest"],
    }


def replay_ev() -> list:
    ev_dir = PROJECT_ROOT / "ev"
    sys.path.insert(0, str(ev_dir))
    os.chdir(ev_dir)
    try:
        import experiment_ev as ev
        rows = ev.run_replay_all()
    finally:
        os.chdir(PROJECT_ROOT)
    return rows


import paths  # noqa: E402  (results layout)
def main():
    paths.ensure_dirs()
    rows = []

    jobs = [(paths.hvac_trace("trace_stress_64.json"), lambda p: record_stress(64, p)),
            (paths.hvac_trace("trace_governance_clean_op_gt_occ.json"),
             lambda p: record_governance_clean("op > occ", {"emergency": 0, "operator": 1, "occupant": 2}, p)),
            (paths.hvac_trace("trace_governance_clean_occ_gt_op.json"),
             lambda p: record_governance_clean("occ > op", {"emergency": 0, "occupant": 1, "operator": 2}, p))]
    for trace_path, record in jobs:
        record(trace_path)
        s = replay_stress(trace_path)
        rows.append(s)
        print(f"{s['workload']:<28} enabled={s['enabled_recorded']}/{s['enabled_replayed']}  "
              f"schedule={'match' if s['schedule_match'] else 'MISMATCH'}  "
              f"decisions={s['decisions_compared']}/{s['decisions_compared'] if s['decisions_match'] else 'MISMATCH'}  "
              f"digest={'match' if s['digest_match'] else 'MISMATCH'}")

    for e in replay_ev():
        rows.append(e)
        print(f"{e['workload']:<28} enabled={e['enabled_recorded']}/{e['enabled_replayed']}  "
              f"schedule={'match' if e['schedule_match'] else 'MISMATCH'}  "
              f"decisions={e['decisions_compared']}/{e['decisions_compared'] if e['decisions_match'] else 'MISMATCH'}  "
              f"digest={'match' if e['digest_match'] else 'MISMATCH'}")

    with open(paths.hvac("experiment_replay_extra.json"), "w", encoding="utf-8") as fh:
        json.dump({"rows": rows}, fh, indent=2)
    print("\nWrote", paths.relative(paths.hvac("experiment_replay_extra.json")))
    if not all(r["schedule_match"] and r["decisions_match"] and r["accepted_match"] and r["digest_match"]
               and r.get("dependencies_match", True) and r.get("cross_process_digest_match", True) for r in rows):
        raise SystemExit("replay mismatch")


if __name__ == "__main__":
    main()
