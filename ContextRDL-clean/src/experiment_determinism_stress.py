"""
Determinism stress test.

The existing determinism experiment runs the same deterministic engine 30
times with no source of randomness and reports a single unique output.  That
is a tautology: the engine has nothing to randomise.  The real question is
whether the *scheduling key* is expressive enough to break ties that would
otherwise produce different commitments, over workloads that do contain ties.

This experiment:
  * Generates synthetic events that all fire rules tied on (roleRank, priority,
    tsKey).  Only (rid, bindKey, aid) break the tie.  N such events means N
    candidate actions all writing to the same (zone, predicate).
  * Runs two schedulers 30 times each:
       - OURS:   sort by the full schedule key (deterministic across runs).
       - SHUFFLE: shuffle the candidates before resolving (i.e. what happens
         if the schedule key is not total and the collection order leaks in).
  * Reports unique final successor digests across 30 runs for N in {2,4,8,16,32}.

Expected:
  * OURS -> 1 unique state for every N.
  * SHUFFLE -> unique-states grows with N (bounded above by min(N, 30)).
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import random
import statistics
from typing import Any, Dict, List, Mapping

from rdflib import Graph, Literal, URIRef

from resolver import resolve_actions as resolve_original
from rule_engine import schedule_actions


SYNTH_ZONE = "http://example.org/building#SynthZone"
SYNTH_ZONE_URI = URIRef(SYNTH_ZONE)
SYNTH_PRED = "http://example.org/building#currentSetpoint"
TIMESTAMP_KEY = 0  # all tied on tsKey


def _graph_digest(graph: Graph) -> str:
    lines = sorted(f"{s.n3()} {p.n3()} {o.n3()} ." for s, p, o in graph)
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def _synth_graph() -> Graph:
    """
    A minimal graph with a single zone the synthetic actions target, meeting
    all SHACL shape cardinality constraints but irrelevant to the invariants
    that would otherwise reject specific values (we keep values in [18,26]).
    """
    from rdflib.namespace import XSD

    g = Graph()
    ex = "http://example.org/building#"
    g.add((SYNTH_ZONE_URI, URIRef("http://www.w3.org/1999/02/22-rdf-syntax-ns#type"),
           URIRef(ex + "HVAC_Zone")))
    g.add((SYNTH_ZONE_URI, URIRef(ex + "currentSetpoint"),
           Literal(20.0, datatype=XSD.decimal)))
    g.add((SYNTH_ZONE_URI, URIRef(ex + "ventilationMode"), Literal("normal")))
    g.add((SYNTH_ZONE_URI, URIRef(ex + "emergencyState"), Literal(False, datatype=XSD.boolean)))
    g.add((SYNTH_ZONE_URI, URIRef(ex + "occupied"), Literal(True, datatype=XSD.boolean)))
    g.add((SYNTH_ZONE_URI, URIRef(ex + "co2Level"), Literal(500, datatype=XSD.decimal)))

    # Policy node (so policy-guard defaults don't trip)
    policy = URIRef(ex + "Policy")
    g.add((policy, URIRef("http://www.w3.org/1999/02/22-rdf-syntax-ns#type"), URIRef(ex + "ControlPolicy")))
    g.add((policy, URIRef(ex + "occupantMaxSetpoint"), Literal(26, datatype=XSD.decimal)))
    g.add((policy, URIRef(ex + "operatorMaxSetpoint"), Literal(26, datatype=XSD.decimal)))
    g.add((policy, URIRef(ex + "emergencyMaxSetpoint"), Literal(26, datatype=XSD.decimal)))
    g.add((policy, URIRef(ex + "minSetpoint"), Literal(18, datatype=XSD.decimal)))
    return g


def _synth_actions(n: int) -> List[Dict[str, Any]]:
    """
    n candidate actions all targeting (SynthZone, currentSetpoint).  They are
    all admissible (values in [18.0, 26.0]) and all tied on (roleRank, priority,
    tsKey).  Distinguishing fields are (rid, bindKey, aid).
    """
    actions = []
    for i in range(n):
        # Use predictable but distinct rid/bindKey/aid
        rid = f"r_synth_{i:04d}"
        bind_key = json.dumps({"i": i}, sort_keys=True, separators=(",", ":"))
        payload = {
            "rid": rid,
            "eid": f"synth-evt-{i:04d}",
            "window_id": "urn:window",
            "predicate": SYNTH_PRED,
            "bindings": {"i": i},
        }
        aid = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        # Values cycle in [18.0, 26.0].  Two actions with the same value still
        # have different aids, so they all count as distinct candidates.
        value = 18.0 + (i % 9)
        actions.append({
            "aid": aid,
            "rid": rid,
            "event_uri": f"urn:event:synth-evt-{i:04d}",
            "event_id": f"synth-evt-{i:04d}",
            "event_ts": "2026-03-14T09:00:00Z",
            "event_role": "operator",
            "window_id": "urn:window",
            "zone": SYNTH_ZONE,
            "predicate": SYNTH_PRED,
            "target_key": f"{SYNTH_ZONE}|{SYNTH_PRED}",
            "value": value,
            "priority": 1,
            "role": "operator",
            "roleRank": 1,
            "tsKey": TIMESTAMP_KEY,
            "bindKey": bind_key,
            "bindings": {"i": i},
        })
    return actions


def _settings() -> Dict[str, Any]:
    return {
        "governance": {"conflict_policy": "first_writer_wins"},
        "schedule_key": ["roleRank", "priority", "tsKey", "rid", "bindKey", "aid"],
    }


def _run_ours(base: Graph, actions: List[Dict]) -> str:
    schedule = schedule_actions(actions, settings=_settings())
    _accepted, successor, _decisions = resolve_original(
        base, schedule, shapes_path="shapes/invariants.ttl", settings=_settings()
    )
    return _graph_digest(successor)


def _run_shuffle(base: Graph, actions: List[Dict], rng: random.Random) -> str:
    """Baseline: collection order leaks in; no tie-breaking beyond shuffle."""
    perm = list(actions)
    rng.shuffle(perm)
    # Still call resolver with first-writer-wins -- the difference is that the
    # winner now depends on whichever candidate happens to come first.
    _accepted, successor, _decisions = resolve_original(
        base, perm, shapes_path="shapes/invariants.ttl", settings=_settings()
    )
    return _graph_digest(successor)


def run_for_n(n: int, runs: int = 30) -> Dict[str, Any]:
    base = _synth_graph()
    actions = _synth_actions(n)

    ours_digests = set()
    for _ in range(runs):
        ours_digests.add(_run_ours(base, copy.deepcopy(actions)))

    rng = random.Random(12345 + n)
    shuffle_digests = set()
    for _ in range(runs):
        shuffle_digests.add(_run_shuffle(base, copy.deepcopy(actions), rng))

    return {
        "n_candidates":              n,
        "runs_per_strategy":         runs,
        "unique_states_ours":        len(ours_digests),
        "unique_states_shuffle":     len(shuffle_digests),
    }


def main():
    results = [run_for_n(n) for n in (2, 4, 8, 16, 32, 64)]

    os.makedirs("results", exist_ok=True)
    with open("results/experiment_determinism_stress.json", "w", encoding="utf-8") as fh:
        json.dump({"results": results}, fh, indent=2)

    print(f"{'N':>4s}  {'unique(ours)':>14s}  {'unique(shuffle)':>16s}")
    for r in results:
        print(f"{r['n_candidates']:>4d}  {r['unique_states_ours']:>14d}  {r['unique_states_shuffle']:>16d}")
    print("\nWrote results/experiment_determinism_stress.json")


if __name__ == "__main__":
    main()
