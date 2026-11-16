"""Record the agreed ordering experiments using the real ContextRDL resolver.

Place this file in src/ and run from the repository root:

    python src/experiment_exhaustive_orders.py

The paper configuration resolves EVERY gated order with reference SHACL.
--regime incremental is an optional diagnostic run, not the paper configuration.
Final-state SHACL checks and representative replays alone do not establish
agreement at every intermediate state of an incremental enumeration.

The script also runs ungated orders, strict timestamp permutations, and the
existing tie fixture with each candidate first. It prints named placeholders
from the measured outputs, records successor snapshots and changed triples,
and contains no expected empirical successor counts, rates, or digest values.
Interpretation sentences must be written after reviewing the recorded results.
"""
from __future__ import annotations

import argparse
import itertools
import json
import math
import os
import sys
import time
from collections import Counter
from copy import deepcopy
from datetime import timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import experiment_hvac_v3 as hvac  # noqa: E402; sets the repository directory
import experiment_determinism_stress as tie  # noqa: E402
from admissibility import check_admissibility_shacl  # noqa: E402
from dataset_builder import load_events, parse_timestamp, select_window_events  # noqa: E402
from rdflib import Graph, Literal, URIRef  # noqa: E402
from resolver import resolve_actions  # noqa: E402
from state_transition import apply_action  # noqa: E402
from trace import (  # noqa: E402
    _environment, file_sha256, graph_delta, graph_digest, serialize_graph_snapshot,
)

SHAPES = hvac.SHAPES
ZONE_C = URIRef("http://example.org/building#ZoneC")
EMERGENCY_STATE = URIRef("http://example.org/building#emergencyState")
TIE_SIZES = (2, 4, 8, 16, 32, 64)  # Existing manuscript workload sizes.


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def copy_graph(graph):
    copied = Graph()
    for triple in graph:
        copied.add(triple)
    return copied


def emergency_true(graph):
    return any(isinstance(value, Literal) and value.toPython() is True
               for value in graph.objects(ZONE_C, EMERGENCY_STATE))


def action_ids(actions):
    return [str(action["aid"]) for action in actions]


def action_records(actions):
    return json.loads(json.dumps(list(actions), sort_keys=True, default=str))


def percentage(numerator, denominator):
    return round(100.0 * numerator / denominator, 4) if denominator else None


def progress(label, completed, total, interval, started):
    if completed == total or (interval and completed % interval == 0):
        print(f"{label}: {completed}/{total}; elapsed {time.perf_counter() - started:.1f} s",
              flush=True)


def successor_records(counts, graphs, representatives, initial, fixed=None):
    records = []
    for digest, count in sorted(counts.items(), key=lambda item: (-item[1], item[0])):
        graph = graphs[digest]
        conforms, report = check_admissibility_shacl(graph, SHAPES)
        removed, inserted = graph_delta(initial, graph)
        record = {
            "digest": digest,
            "orders": count,
            "admissible": bool(conforms),
            "zoneC_emergency_true": emergency_true(graph),
            "representative": representatives[digest],
            "successor_graph": serialize_graph_snapshot(graph),
            "changes_from_input": {"removed": removed, "inserted": inserted},
        }
        if fixed is not None:
            removed, inserted = graph_delta(fixed, graph)
            record["changes_from_fixed_successor"] = {"removed": removed, "inserted": inserted}
        if not conforms:
            record["validation_report"] = str(report)
        records.append(record)
    return records


def summarize_orders(records, fixed_digest):
    return {
        "orders": sum(row["orders"] for row in records),
        "distinct_successors": len(records),
        "admissible_successors": sum(row["admissible"] for row in records),
        "admissible_orders": sum(row["orders"] for row in records if row["admissible"]),
        "orders_equal_to_fixed_schedule": sum(row["orders"] for row in records
                                             if row["digest"] == fixed_digest),
        "orders_with_zoneC_emergency_true": sum(row["orders"] for row in records
                                                if row["zoneC_emergency_true"]),
        "successors": records,
    }


def enumerate_with_gates(state, schedule, settings, regime, fixed_graph, interval):
    os.environ["ADMISSIBILITY_REGIME"] = regime
    counts, graphs, representatives = Counter(), {}, {}
    total, started = math.factorial(len(schedule)), time.perf_counter()
    print(f"Gated enumeration starts with validator: {regime}", flush=True)
    for index, order in enumerate(itertools.permutations(schedule), 1):
        # The real resolver clones its input; the extra copy makes each run's
        # starting graph explicit and protects the stored initial snapshot.
        successor, _ = hvac.strat_ours(copy_graph(state), list(order), settings)
        digest = graph_digest(successor)
        counts[digest] += 1
        if digest not in graphs:
            graphs[digest] = successor
            representatives[digest] = {"action_ids": action_ids(order),
                                       "rule_ids": [action["rid"] for action in order]}
        progress("Gated orders", index, total, interval, started)

    records = successor_records(counts, graphs, representatives, state, fixed_graph)
    os.environ["ADMISSIBILITY_REGIME"] = "shacl"
    by_aid = {str(action["aid"]): action for action in schedule}
    for record in records:
        order = [by_aid[aid] for aid in record["representative"]["action_ids"]]
        accepted, successor, decisions = resolve_actions(
            copy_graph(state), order, shapes_path=SHAPES, settings=settings)
        record["reference_replay_digest"] = graph_digest(successor)
        record["reference_replay_matches"] = record["reference_replay_digest"] == record["digest"]
        record["reference_accepted_action_ids"] = action_ids(accepted)
        record["reference_decisions"] = decisions

    result = summarize_orders(records, graph_digest(fixed_graph))
    result.update({
        "regime_for_enumeration": regime,
        "permutations_from_candidate_count": total,
        "reference_shacl_confirms_representatives": all(row["reference_replay_matches"]
                                                       for row in records),
    })
    return result


def enumerate_without_gates(state, enabled, fixed_graph, interval):
    counts, graphs, representatives = Counter(), {}, {}
    candidates = sorted(enabled, key=lambda action: str(action["aid"]))
    total, started = math.factorial(len(candidates)), time.perf_counter()
    print("Ungated enumeration starts; final successors are checked with reference SHACL.", flush=True)
    for index, order in enumerate(itertools.permutations(candidates), 1):
        successor = copy_graph(state)
        for action in order:
            successor = apply_action(successor, action)
        digest = graph_digest(successor)
        counts[digest] += 1
        if digest not in graphs:
            graphs[digest] = successor
            representatives[digest] = {"action_ids": action_ids(order),
                                       "rule_ids": [action["rid"] for action in order]}
        progress("Ungated orders", index, total, interval, started)
    records = successor_records(counts, graphs, representatives, state, fixed_graph)
    result = summarize_orders(records, graph_digest(fixed_graph))
    result["permutations_from_candidate_count"] = total
    return result


def timestamp_orders(events, initial_state, fixed_graph, interval):
    os.environ["ADMISSIBILITY_REGIME"] = "shacl"
    require(bool(events), "The timestamp experiment requires the default events.")
    base = min(parse_timestamp(str(event["timestamp"])) for event in events)
    original_digest = graph_digest(fixed_graph)
    counts, graphs, representatives = Counter(), {}, {}
    total, started = math.factorial(len(events)), time.perf_counter()
    same_initial_graph = True
    all_input_events_selected = True
    event_ids = sorted(str(event["eid"]) for event in events)
    print("Timestamp experiment starts with reference SHACL.", flush=True)
    for index, offsets in enumerate(itertools.permutations(range(len(events))), 1):
        shifted = deepcopy(events)
        for event, offset in zip(shifted, offsets):
            event["timestamp"] = (base + timedelta(seconds=offset)).isoformat().replace("+00:00", "Z")
        # Rebuild the dataset, re-evaluate rules, construct and schedule actions.
        state, _, schedule, settings = hvac.pipeline(shifted)
        selected, window = select_window_events(shifted, settings=settings)
        all_input_events_selected &= sorted(str(event["eid"]) for event in selected) == event_ids
        same_initial_graph &= graph_digest(state) == graph_digest(initial_state)
        successor, _ = hvac.strat_ours(state, schedule, settings)
        digest = graph_digest(successor)
        counts[digest] += 1
        if digest not in graphs:
            graphs[digest] = successor
            representatives[digest] = {
                "timestamps": {str(event["eid"]): event["timestamp"] for event in shifted},
                "window": window,
                "action_ids": action_ids(schedule),
                "rule_ids": [action["rid"] for action in schedule],
            }
        progress("Timestamp assignments", index, total, interval, started)
    return {
        "regime": "shacl",
        "event_count": len(events),
        "first_timestamp": base.isoformat(),
        "timestamp_spacing_seconds": 1,
        "timestamp_orders": sum(counts.values()),
        "permutations_from_event_count": total,
        "distinct_successors": len(counts),
        "original_successor_digest": original_digest,
        "orders_equal_to_original_successor": counts.get(original_digest, 0),
        "all_successors_equal_original": all(digest == original_digest for digest in counts),
        "same_initial_graph": same_initial_graph,
        "all_input_events_selected": all_input_events_selected,
        "successors": successor_records(counts, graphs, representatives, initial_state, fixed_graph),
    }


def exact_tie_counts():
    os.environ["ADMISSIBILITY_REGIME"] = "shacl"
    results = []
    print("Exact tie counts start with the original synthetic fixture and reference SHACL.", flush=True)
    for n in TIE_SIZES:
        state, candidates, settings = tie._synth_graph(), tie._synth_actions(n), tie._settings()
        require(len(set(action_ids(candidates))) == len(candidates), "Tie fixture has repeated action IDs.")
        require(len({action["target_key"] for action in candidates}) == 1,
                "Tie fixture no longer targets a single shared slot.")
        counts, graphs, representatives = Counter(), {}, {}
        for first in range(len(candidates)):
            order = deepcopy([candidates[first]] + candidates[:first] + candidates[first + 1:])
            accepted, successor, _ = resolve_actions(
                copy_graph(state), order, shapes_path=SHAPES, settings=settings)
            require(len(accepted) == 1,
                    f"N={n}: expected a sole accepted action for the tie method; found {len(accepted)}.")
            require(accepted[0]["aid"] == order[0]["aid"],
                    f"N={n}: the sole accepted action is not the first candidate.")
            digest = graph_digest(successor)
            counts[digest] += 1
            if digest not in graphs:
                graphs[digest] = successor
                representatives[digest] = {"first_action_id": accepted[0]["aid"],
                                           "action_ids": action_ids(order)}
        results.append({
            "n_candidates": n,
            "regime": "shacl",
            "resolver_runs": sum(counts.values()),
            "first_candidate_checked_as_sole_accepted": True,
            "distinct_successors": len(counts),
            "initial_graph": serialize_graph_snapshot(state),
            "settings": settings,
            "candidate_actions": action_records(candidates),
            "successors": successor_records(counts, graphs, representatives, state),
        })
        print(f"Tie fixture N={n}: completed {len(candidates)} first-candidate runs.", flush=True)
    return results


def named_placeholders(gated, ungated, stamps, ties):
    values = {}
    for prefix, result in (("G", gated), ("U", ungated)):
        values.update({
            prefix + "N": result["orders"],
            prefix + "S": result["distinct_successors"],
            prefix + "V": result["admissible_successors"],
            prefix + "A": result["admissible_orders"],
            prefix + "AP": percentage(result["admissible_orders"], result["orders"]),
            prefix + "F": result["orders_equal_to_fixed_schedule"],
            prefix + "FP": percentage(result["orders_equal_to_fixed_schedule"], result["orders"]),
            prefix + "E": result["orders_with_zoneC_emergency_true"],
            prefix + "EP": percentage(result["orders_with_zoneC_emergency_true"], result["orders"]),
        })
    values.update({f"R{row['n_candidates']}": row["distinct_successors"] for row in ties})
    values.update({"TSN": stamps["timestamp_orders"], "TSS": stamps["distinct_successors"]})
    return values


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--regime", choices=["incremental", "shacl"], default="shacl")
    parser.add_argument("--progress-every", type=int, default=500,
                        help="Print progress after this many orders; zero prints phase completion only.")
    args = parser.parse_args()
    require(args.progress_every >= 0, "--progress-every must be nonnegative.")
    os.environ["ADMISSIBILITY_REGIME"] = "shacl"

    events = load_events("data/events.jsonl")
    state, enabled, schedule, settings = hvac.pipeline(events)
    require(len(set(action_ids(schedule))) == len(schedule), "The schedule has repeated action IDs.")
    canonical = lambda actions: sorted(json.dumps(a, sort_keys=True, default=str) for a in actions)
    require(canonical(enabled) == canonical(schedule), "Gated and ungated candidate sets differ.")
    input_admissible, report = check_admissibility_shacl(state, SHAPES)
    require(input_admissible, "The default input graph is inadmissible:\n" + str(report))
    accepted, fixed_graph, fixed_decisions = resolve_actions(
        copy_graph(state), schedule, shapes_path=SHAPES, settings=settings)
    input_digest = graph_digest(state)

    input_paths = ("data/events.jsonl", "data/base_graph.ttl", "data/contexts.json",
                   "configs/settings.json", "configs/rules.json", SHAPES)
    result = {
        "status": "running",
        "configuration": {
            "regime_for_enumeration": args.regime,
            "paper_configuration": args.regime == "shacl",
            "timestamp_and_tie_regime": "shacl",
            "percentages_rounded_to_decimal_places": 4,
            "settings": settings,
            "environment": _environment(),
            "input_files_sha256_normalized_lf": {p: file_sha256(p) for p in input_paths},
        },
        "original_events": events,
        "initial_graph": serialize_graph_snapshot(state),
        "candidate_actions": action_records(enabled),
        "fixed_schedule_action_ids": action_ids(schedule),
        "fixed_schedule_digest": graph_digest(fixed_graph),
        "fixed_schedule_successor": serialize_graph_snapshot(fixed_graph),
        "fixed_schedule_accepted_action_ids": action_ids(accepted),
        "fixed_schedule_decisions": fixed_decisions,
    }
    out_file = Path("results/hvac/experiment_exhaustive_orders.json")
    out_file.parent.mkdir(parents=True, exist_ok=True)

    try:
        if args.regime != "shacl":
            print("DIAGNOSTIC CONFIGURATION: incremental enumeration is not the paper run.", flush=True)
        result["all_four_gates"] = enumerate_with_gates(
            state, schedule, settings, args.regime, fixed_graph, args.progress_every)
        result["no_gates"] = enumerate_without_gates(state, enabled, fixed_graph, args.progress_every)
        result["timestamps"] = timestamp_orders(events, state, fixed_graph, args.progress_every)
        result["tie_exact"] = exact_tie_counts()
        gated, ungated, stamps = result["all_four_gates"], result["no_gates"], result["timestamps"]
        result["placeholders"] = named_placeholders(gated, ungated, stamps, result["tie_exact"])
        result["checks"] = {
            "gated_permutation_count": gated["orders"] == gated["permutations_from_candidate_count"],
            "ungated_permutation_count": ungated["orders"] == ungated["permutations_from_candidate_count"],
            "timestamp_permutation_count": stamps["timestamp_orders"] == stamps["permutations_from_event_count"],
            "initial_graph_unchanged": graph_digest(state) == input_digest,
            "all_gated_successors_admissible": all(row["admissible"] for row in gated["successors"]),
            "reference_representatives_match": gated["reference_shacl_confirms_representatives"],
            "fixed_reference_successor_reached": gated["orders_equal_to_fixed_schedule"] > 0,
            "timestamp_same_initial_graph": stamps["same_initial_graph"],
            "timestamp_all_input_events_selected": stamps["all_input_events_selected"],
            "timestamp_successors_equal_original": stamps["all_successors_equal_original"],
            "tie_first_candidate_is_sole_accepted": all(row["first_candidate_checked_as_sole_accepted"]
                                                       for row in result["tie_exact"]),
            "input_files_unchanged": all(file_sha256(p) == digest for p, digest
                                         in result["configuration"]["input_files_sha256_normalized_lf"].items()),
        }
        result["checks_requiring_review"] = [name for name, passed in result["checks"].items() if not passed]
        result["status"] = "completed_requires_review" if result["checks_requiring_review"] else "completed"
    except Exception as error:
        result["status"] = "failed"
        result["error"] = {"type": type(error).__name__, "message": str(error)}
        raise
    finally:
        out_file.write_text(json.dumps(result, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
        print("Wrote", out_file, flush=True)

    print("\nCHECKS")
    print("regime_for_enumeration:", args.regime)
    print(json.dumps(result["checks"], indent=2))
    print("\nNUMERIC PLACEHOLDERS (recorded outputs, not prefilled manuscript results)")
    for name, value in result["placeholders"].items():
        print(f"[{name}] = {value}")
    print("\nInspect successor snapshots, changes_from_input, changes_from_fixed_successor,")
    print("and reference_decisions in the JSON before writing interpretation sentences.")
    if result["checks_requiring_review"]:
        raise SystemExit("A comparison or consistency check needs investigation. Keep the manuscript placeholders pending.")
    if args.regime != "shacl":
        print("This was a diagnostic run. Use the reference-SHACL configuration for the paper.")


if __name__ == "__main__":
    main()
