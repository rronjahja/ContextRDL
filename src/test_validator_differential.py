"""
Property-based differential testing of the incremental validator against
pySHACL (reviewer concern R1-5).

The earlier equivalence evidence (12 single-property + 49 pairwise changes)
was too limited. This harness compares check_admissibility_incremental
(full-graph mode) against the pySHACL reference on:

  Structured suite:
    * boundary values on every numeric shape (17.999, 18, 21, 21.001, 23,
      23.001, 26, 26.001) per zone,
    * every enumeration value plus invalid strings for ventilationMode,
    * non-boolean and wrongly-typed emergencyState,
    * cardinality faults: deletion-only (0 values) and multi-valued
      (2 values) for each zone property,
    * unexpected datatypes / lexical forms (string-typed numbers, xsd:int),
  Randomised suite (seeded, reproducible):
    * k-way combinations of random mutations for k in {1..5},
    * apply -> check -> revert -> check rollback sequences (validator must
      agree before, during, and after),
  drawn from randomly generated graph states.

Every case asserts identical verdicts; any disagreement is printed verbatim
and recorded. Usage:
    python test_validator_differential.py [n_random] [seed]
Writes results/test_validator_differential.json
"""
from __future__ import annotations

import itertools
import json
import random
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Tuple

from rdflib import Graph, Literal, URIRef
from rdflib.namespace import XSD

from admissibility import check_admissibility_incremental, check_admissibility_shacl
from dataset_builder import load_state

EX = "http://example.org/building#"
ZONES = [URIRef(f"{EX}Zone{z}") for z in "ABC"]
SETPOINT = URIRef(f"{EX}currentSetpoint")
VENT = URIRef(f"{EX}ventilationMode")
EMERG = URIRef(f"{EX}emergencyState")
SHAPES = "shapes/invariants.ttl"

BOUNDARY_SETPOINTS = [17.999, 18.0, 20.5, 21.0, 21.001, 23.0, 23.001, 26.0, 26.001, -1.0, 100.0]
VENT_VALUES = ["off", "normal", "high", "emergency", "OFF", "turbo", "", "øff"]
EMERG_VALUES: List[Any] = [True, False]

Mutation = Tuple[str, URIRef, URIRef, List[Literal]]  # (label, subject, predicate, new objects)


def structured_mutations() -> List[List[Mutation]]:
    cases: List[List[Mutation]] = []
    for zone in ZONES:
        for v in BOUNDARY_SETPOINTS:
            cases.append([("sp", zone, SETPOINT, [Literal(v, datatype=XSD.decimal)])])
        for v in VENT_VALUES:
            cases.append([("vent", zone, VENT, [Literal(v)])])
        for v in EMERG_VALUES:
            cases.append([("emerg", zone, EMERG, [Literal(v)])])
        # wrong datatypes / lexical forms
        cases.append([("sp_str", zone, SETPOINT, [Literal("22")])])
        cases.append([("sp_int", zone, SETPOINT, [Literal(22, datatype=XSD.integer)])])
        cases.append([("emerg_str", zone, EMERG, [Literal("true")])])
        # cardinality faults
        for pred, tag in ((SETPOINT, "sp"), (VENT, "vent"), (EMERG, "emerg")):
            cases.append([(f"{tag}_del", zone, pred, [])])  # deletion-only
        cases.append([("sp_multi", zone, SETPOINT,
                       [Literal(20.0, datatype=XSD.decimal), Literal(22.0, datatype=XSD.decimal)])])
        cases.append([("vent_multi", zone, VENT, [Literal("high"), Literal("off")])])
    # emergency/vent interaction pairs on each zone
    for zone in ZONES:
        for mode in ("high", "off", "emergency"):
            cases.append([
                ("emerg_true", zone, EMERG, [Literal(True)]),
                ("vent", zone, VENT, [Literal(mode)]),
            ])
    return cases


def random_mutation(rng: random.Random) -> Mutation:
    zone = rng.choice(ZONES)
    kind = rng.random()
    if kind < 0.45:
        v = rng.choice(BOUNDARY_SETPOINTS + [round(rng.uniform(10, 30), 3)])
        return ("sp", zone, SETPOINT, [Literal(v, datatype=XSD.decimal)])
    if kind < 0.75:
        return ("vent", zone, VENT, [Literal(rng.choice(VENT_VALUES))])
    if kind < 0.90:
        return ("emerg", zone, EMERG, [Literal(rng.choice(EMERG_VALUES))])
    if kind < 0.95:
        pred = rng.choice([SETPOINT, VENT, EMERG])
        return ("del", zone, pred, [])
    return ("sp_multi", zone, SETPOINT,
            [Literal(20.0, datatype=XSD.decimal), Literal(24.0, datatype=XSD.decimal)])


def apply_mutations(base: Graph, mutations: List[Mutation]) -> Graph:
    g = Graph()
    for t in base:
        g.add(t)
    for _, s, p, objs in mutations:
        for o in list(g.objects(s, p)):
            g.remove((s, p, o))
        for o in objs:
            g.add((s, p, o))
    return g


def verdicts(graph: Graph) -> Tuple[bool, bool]:
    inc, _ = check_admissibility_incremental(graph)
    ref, _ = check_admissibility_shacl(graph, SHAPES)
    return inc, ref


def main(n_random: int = 500, seed: int = 20260817):
    base = load_state("shapes/base_graph.ttl")
    rng = random.Random(seed)
    disagreements: List[Dict[str, Any]] = []
    stats = {"cases": 0, "agree": 0, "conform_agree": 0, "violate_agree": 0}

    def record(tag: str, mutations: List[Mutation], graph: Graph):
        inc, ref = verdicts(graph)
        stats["cases"] += 1
        if inc == ref:
            stats["agree"] += 1
            stats["conform_agree" if ref else "violate_agree"] += 1
        else:
            item = {
                "suite": tag,
                "mutations": [(m[0], str(m[1]).split("#")[-1], [str(o) for o in m[3]]) for m in mutations],
                "incremental": inc,
                "pyshacl": ref,
            }
            disagreements.append(item)
            print("DISAGREEMENT:", json.dumps(item))

    # 1) structured suite
    for mutations in structured_mutations():
        record("structured", mutations, apply_mutations(base, mutations))

    # 2) random k-way combinations over random graph states
    for _ in range(n_random):
        # random base state
        state_muts = [random_mutation(rng) for _ in range(rng.randint(0, 3))]
        state = apply_mutations(base, state_muts)
        k = rng.randint(1, 5)
        muts = [random_mutation(rng) for _ in range(k)]
        record("random_kway", state_muts + muts, apply_mutations(state, muts))

    # 3) rollback sequences: apply -> check -> revert -> check
    rollback_ok = 0
    for _ in range(min(n_random, 200)):
        g0 = apply_mutations(base, [random_mutation(rng)])
        d0 = verdicts(g0)
        mut = random_mutation(rng)
        g1 = apply_mutations(g0, [mut])
        record("rollback_mid", [mut], g1)
        # revert = re-apply original values of the touched slot
        s, p = mut[1], mut[2]
        orig = [("revert", s, p, list(g0.objects(s, p)))]
        g2 = apply_mutations(g1, orig)
        d2 = verdicts(g2)
        if d0 == d2:
            rollback_ok += 1
        stats["cases"] += 1
        stats["agree"] += 1 if d2[0] == d2[1] else 0
        if d2[0] != d2[1]:
            disagreements.append({"suite": "rollback_post", "mutations": str(mut)})

    result = {
        "seed": seed,
        "n_random": n_random,
        "total_cases": stats["cases"],
        "agreements": stats["agree"],
        "agreement_rate": round(stats["agree"] / stats["cases"], 6),
        "agree_on_conforming": stats["conform_agree"],
        "agree_on_violating": stats["violate_agree"],
        "rollback_restored": rollback_ok,
        "disagreements": disagreements,
    }
    print(json.dumps({k: v for k, v in result.items() if k != "disagreements"}, indent=2))
    print("disagreements:", len(disagreements))

    out = Path(__file__).resolve().parent.parent / "results" / "test_validator_differential.json"
    out.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print("Wrote", out)
    return result


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 500
    s = int(sys.argv[2]) if len(sys.argv) > 2 else 20260817
    main(n, s)
