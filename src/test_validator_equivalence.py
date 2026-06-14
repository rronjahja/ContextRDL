"""
Regression test: pySHACL and the incremental validator must agree on every
admissibility verdict for perturbations of the base graph.

If this test FAILS, do not trust the incremental validator -- it means the
hand-coded Python checks in ``admissibility.check_admissibility_incremental``
have drifted from the SHACL shapes in ``shapes/invariants.ttl``.

Run:
    python test_validator_equivalence.py
Exit code 0 on success, 1 on any disagreement.
"""
from __future__ import annotations

import itertools
import json
import sys
from typing import Dict, List

from rdflib import Graph, Literal, URIRef
from rdflib.namespace import XSD

from admissibility import check_admissibility_incremental, check_admissibility_shacl

EX = "http://example.org/building#"
BASE_GRAPH_PATH = "shapes/base_graph.ttl"
SHAPES_PATH = "shapes/invariants.ttl"


def load_base() -> Graph:
    g = Graph()
    g.parse(BASE_GRAPH_PATH, format="turtle")
    return g


def apply_override(graph: Graph, subject: URIRef, predicate: URIRef, new_value) -> Graph:
    out = Graph()
    for t in graph:
        out.add(t)
    for t in list(out.triples((subject, predicate, None))):
        out.remove(t)
    out.add((subject, predicate, new_value))
    return out


# (label, subject, predicate, new_value, expected_admissible_on_base_graph)
PERTURBATIONS = [
    ("setpoint_below_min_ZoneA",
     URIRef(EX + "ZoneA"), URIRef(EX + "currentSetpoint"),
     Literal(17.0, datatype=XSD.decimal), False),
    ("setpoint_above_max_ZoneA",
     URIRef(EX + "ZoneA"), URIRef(EX + "currentSetpoint"),
     Literal(27.0, datatype=XSD.decimal), False),
    ("setpoint_above_ZoneA_cap",
     URIRef(EX + "ZoneA"), URIRef(EX + "currentSetpoint"),
     Literal(24.0, datatype=XSD.decimal), False),
    ("setpoint_at_ZoneA_cap_boundary",
     URIRef(EX + "ZoneA"), URIRef(EX + "currentSetpoint"),
     Literal(23.0, datatype=XSD.decimal), True),
    ("vent_off_with_high_setpoint_ZoneA",
     URIRef(EX + "ZoneA"), URIRef(EX + "ventilationMode"),
     Literal("off"), False),
    ("vent_invalid_mode_ZoneB",
     URIRef(EX + "ZoneB"), URIRef(EX + "ventilationMode"),
     Literal("turbo"), False),
    ("emergency_true_but_vent_normal_ZoneC",
     URIRef(EX + "ZoneC"), URIRef(EX + "emergencyState"),
     Literal(True, datatype=XSD.boolean), False),
    ("setpoint_at_lower_boundary",
     URIRef(EX + "ZoneB"), URIRef(EX + "currentSetpoint"),
     Literal(18.0, datatype=XSD.decimal), True),
    ("setpoint_at_upper_boundary",
     URIRef(EX + "ZoneB"), URIRef(EX + "currentSetpoint"),
     Literal(26.0, datatype=XSD.decimal), True),
    ("vent_emergency_valid",
     URIRef(EX + "ZoneB"), URIRef(EX + "ventilationMode"),
     Literal("emergency"), True),
    ("setpoint_above_ZoneA_cap_by_one",
     URIRef(EX + "ZoneA"), URIRef(EX + "currentSetpoint"),
     Literal(23.5, datatype=XSD.decimal), False),
    ("vent_off_at_exact_21_ZoneA",
     URIRef(EX + "ZoneA"), URIRef(EX + "currentSetpoint"),
     Literal(21.0, datatype=XSD.decimal), True),  # first set SP to 21, vent is still normal from base
]


def run_singles() -> List[Dict]:
    results = []
    for label, subject, predicate, new_value, expected in PERTURBATIONS:
        perturbed = apply_override(load_base(), subject, predicate, new_value)
        inc_ok, _   = check_admissibility_incremental(perturbed)
        shacl_ok, _ = check_admissibility_shacl(perturbed, SHAPES_PATH)
        results.append({
            "test": label,
            "incremental": inc_ok,
            "shacl": shacl_ok,
            "expected": expected,
            "agree": inc_ok == shacl_ok,
            "expectation_met": (inc_ok == expected) and (shacl_ok == expected),
        })
    return results


def run_pairs() -> List[Dict]:
    results = []
    for a, b in itertools.combinations(PERTURBATIONS, 2):
        l1, s1, p1, v1, _ = a
        l2, s2, p2, v2, _ = b
        if (s1, p1) == (s2, p2):
            continue
        g = apply_override(load_base(), s1, p1, v1)
        g = apply_override(g,          s2, p2, v2)
        inc_ok, _   = check_admissibility_incremental(g)
        shacl_ok, _ = check_admissibility_shacl(g, SHAPES_PATH)
        results.append({
            "test": f"{l1}+{l2}",
            "incremental": inc_ok,
            "shacl": shacl_ok,
            "agree": inc_ok == shacl_ok,
        })
    return results


def main():
    singles = run_singles()
    pairs   = run_pairs()

    single_fails = [r for r in singles if not r["agree"] or not r["expectation_met"]]
    pair_fails   = [r for r in pairs if not r["agree"]]

    print("=== Single-perturbation results ===")
    for r in singles:
        ok = r["agree"] and r["expectation_met"]
        tag = "PASS" if ok else "FAIL"
        print(f"  [{tag}] {r['test']}: inc={r['incremental']} shacl={r['shacl']} expected={r['expected']}")

    print(f"\n=== Pair perturbations: {len(pairs)} total, {len(pair_fails)} disagreements ===")
    for r in pair_fails:
        print(f"  [FAIL] {r['test']}: inc={r['incremental']} shacl={r['shacl']}")

    summary = {
        "singles_total": len(singles),
        "singles_failed": len(single_fails),
        "pairs_total": len(pairs),
        "pairs_failed": len(pair_fails),
        "overall_pass": len(single_fails) == 0 and len(pair_fails) == 0,
        "details": {"singles": singles, "pair_failures": pair_fails},
    }

    import os
    os.makedirs("results", exist_ok=True)
    with open("results/test_validator_equivalence.json", "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2)

    if summary["overall_pass"]:
        print(f"\nALL PASSED: {len(singles)} singles + {len(pairs)} pairs.")
        sys.exit(0)
    else:
        print(f"\nFAILED: {len(single_fails)} single disagreements, {len(pair_fails)} pair disagreements.")
        sys.exit(1)


if __name__ == "__main__":
    main()
