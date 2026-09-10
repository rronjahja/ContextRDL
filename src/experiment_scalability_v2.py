"""
Scalability: reference resolver (graph cloning, full-graph validation per
action, decision-level digests and deltas) versus incremental resolver
(in-place mutation, zone-scoped validation), on N synthetic zone-local
actions. The incremental resolver is timed twice: with the same
decision-level trace profile as the reference (record_digests=True), which
isolates the effect of graph handling and validation scope, and without
per-action digests, which is the cheapest configuration. Provides run_case(n), which
experiment_scalability_sd.py repeats to obtain the mean +/- sd of Table
tab:scalability, and a single-run main() for a quick check.

Workload (identical for every N): N HVAC zones ScaleZone00000..ScaleZone{N-1},
each with currentSetpoint 20.0, ventilationMode "normal", emergencyState
false, occupied false, co2Level 500.0, plus the standard policy node; N
operator PreheatRequest events with target 22 at the same timestamp, one per
zone. Every action writes a distinct target, so nothing is shadowed and every
action is accepted; the two resolvers must agree on the accepted set and on
the successor digest at every N. (The recorded successor digests are
reproducible: N=10 gives 67d43a67..., N=50 gives 045b6e71..., N=800 gives
1fb5461e....)

Run from the project root:
    python src/experiment_scalability_v2.py
Writes results/hvac/experiment_scalability_v2.json and .csv
"""
from __future__ import annotations

import csv
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

HERE = Path(__file__).resolve().parent
if (HERE / "rule_engine.py").exists():
    sys.path.insert(0, str(HERE))
    PROJECT_ROOT = HERE.parent
else:
    PROJECT_ROOT = HERE
import paths  # noqa: E402  (results layout)
os.chdir(PROJECT_ROOT)

from rdflib import Graph, Literal, Namespace  # noqa: E402
from rdflib.namespace import RDF, XSD  # noqa: E402

from dataset_builder import build_dataset  # noqa: E402
from resolver import resolve_actions as resolve_original  # noqa: E402
from resolver_incremental import resolve_actions_incremental  # noqa: E402
from rule_engine import evaluate_rules, load_settings, resolve_governance_context, schedule_actions  # noqa: E402
from rule_loader import load_rules  # noqa: E402
from trace import graph_digest, _environment  # noqa: E402

EX = Namespace("http://example.org/building#")
SHAPES = "shapes/invariants.ttl"
SIZES = [10, 50, 100, 200, 400, 800]


def build_state(num_zones: int) -> Graph:
    g = Graph()
    g.bind("ex", EX)
    for idx in range(num_zones):
        zone = EX[f"ScaleZone{idx:05d}"]
        g.add((zone, RDF.type, EX.HVAC_Zone))
        g.add((zone, EX.currentSetpoint, Literal(20.0, datatype=XSD.decimal)))
        g.add((zone, EX.ventilationMode, Literal("normal")))
        g.add((zone, EX.emergencyState, Literal(False, datatype=XSD.boolean)))
        g.add((zone, EX.occupied, Literal(False, datatype=XSD.boolean)))
        g.add((zone, EX.co2Level, Literal(500.0, datatype=XSD.decimal)))
    g.add((EX.Policy, RDF.type, EX.ControlPolicy))
    g.add((EX.Policy, EX.occupantMaxSetpoint, Literal("23.0", datatype=XSD.decimal)))
    g.add((EX.Policy, EX.operatorMaxSetpoint, Literal("24.0", datatype=XSD.decimal)))
    g.add((EX.Policy, EX.emergencyMaxSetpoint, Literal("26.0", datatype=XSD.decimal)))
    g.add((EX.Policy, EX.minSetpoint, Literal("18.0", datatype=XSD.decimal)))
    return g


def build_events(num_actions: int) -> List[Dict[str, Any]]:
    return [{
        "eid": f"scale-preheat-{idx:05d}",
        "timestamp": "2026-03-14T09:00:00Z",
        "type": "PreheatRequest",
        "role": "operator",
        "payload": {"zone": str(EX[f"ScaleZone{idx:05d}"]), "target": 22},
    } for idx in range(num_actions)]


def run_case(n: int) -> Dict[str, Any]:
    settings = load_settings("configs/settings.json")
    context = resolve_governance_context(settings=settings, contexts_path="data/contexts.json")
    rules = load_rules("configs/rules.json")

    graph_t = build_state(n)
    events = build_events(n)
    dataset, window_meta = build_dataset(graph_t, events, settings=settings)
    enabled = evaluate_rules(dataset, rules, settings=settings, context=context, window_meta=window_meta)
    schedule = schedule_actions(enabled, settings=settings)

    t0 = time.perf_counter()
    accepted_orig, successor_orig, decisions_orig = resolve_original(graph_t, schedule, shapes_path=SHAPES, settings=settings)
    t_orig = time.perf_counter() - t0

    # Incremental resolver under the SAME trace profile as the reference
    # (decision-level digests and deltas recorded), and without them.
    t0 = time.perf_counter()
    accepted_full, successor_full, decisions_full = resolve_actions_incremental(
        graph_t, schedule, shapes_path=SHAPES, settings=settings, record_digests=True)
    t_new_full = time.perf_counter() - t0

    t0 = time.perf_counter()
    accepted_new, successor_new, _ = resolve_actions_incremental(
        graph_t, schedule, shapes_path=SHAPES, settings=settings, record_digests=False)
    t_new = time.perf_counter() - t0

    digest_orig = graph_digest(successor_orig)
    digest_new = graph_digest(successor_new)
    digest_full = graph_digest(successor_full)

    def _records(decisions):
        return [json.loads(json.dumps({k: v for k, v in d.items() if k != "validation_report"},
                                      sort_keys=True, default=str)) for d in decisions]

    return {
        "decisions_match_same_trace_profile": _records(decisions_orig) == _records(decisions_full),
        "actions": n,
        "accepted_orig": len(accepted_orig),
        "accepted_new": len(accepted_new),
        "accepted_match": [a["aid"] for a in accepted_orig] == [a["aid"] for a in accepted_new]
                          == [a["aid"] for a in accepted_full],
        "digest_match": digest_orig == digest_new == digest_full,
        "successor_digest": digest_orig,
        "time_seconds_orig": t_orig,
        "time_seconds_new_full_trace": t_new_full,
        "time_seconds_new": t_new,
        "speedup_same_trace_profile_x": round(t_orig / t_new_full, 2) if t_new_full > 0 else None,
        "speedup_x": round(t_orig / t_new, 2) if t_new > 0 else None,
    }


def main(sizes: List[int] = SIZES) -> None:
    out_dir = paths.HVAC
    paths.ensure_dirs()
    rows = []
    for n in sizes:
        row = run_case(n)
        rows.append(row)
        print(f"N={n:>4d} accepted={row['accepted_new']:>4d} ref={row['time_seconds_orig']:8.3f}s "
              f"incr(same trace)={row['time_seconds_new_full_trace']:7.3f}s ({row['speedup_same_trace_profile_x']}x) "
              f"incr(no digests)={row['time_seconds_new']:7.4f}s ({row['speedup_x']}x) "
              f"digest={row['successor_digest'][:12]} "
              f"[{'OK' if row['accepted_match'] and row['digest_match'] and row['decisions_match_same_trace_profile'] else 'MISMATCH'}]")
    (out_dir / "experiment_scalability_v2.json").write_text(json.dumps({"environment": _environment(),
        "admissibility_regime": os.environ.get("ADMISSIBILITY_REGIME", "incremental"), "rows": rows}, indent=2), encoding="utf-8")
    with open(out_dir / "experiment_scalability_v2.csv", "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    print("Wrote", out_dir / "experiment_scalability_v2.json")
    if not all(r["accepted_match"] and r["digest_match"] and r["decisions_match_same_trace_profile"] for r in rows):
        raise SystemExit("resolver disagreement at some N")


if __name__ == "__main__":
    arg_sizes = [int(a) for a in sys.argv[1:]] or SIZES
    main(arg_sizes)
