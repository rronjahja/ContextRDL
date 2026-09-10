"""Policy-input regressions, including empty windows and fresh hash-seed processes."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "ev")]
os.chdir(ROOT)
import paths
from rdflib import Graph, Literal, URIRef
from rdflib.namespace import XSD
from admissibility import check_admissibility_incremental, check_admissibility_shacl
from dataset_builder import load_state
from engine import run_engine
from independent_resolver import resolve as second_resolve
from resolver import resolve_actions
from resolver_incremental import resolve_actions_incremental
from trace import _environment, graph_digest
import experiment_ev as ev


def malformed(graph, node, predicate, kind):
    if kind == "multiple":
        graph.add((node, predicate, Literal("20", datatype=XSD.decimal)))
    elif kind == "missing_node":
        graph.remove((node, None, None))
    elif kind == "missing_value":
        graph.remove((node, predicate, None))
    elif kind == "wrong_datatype":
        graph.set((node, predicate, Literal(24)))
    elif kind == "nonfinite":
        graph.set((node, predicate, Literal("NaN", datatype=XSD.decimal, normalize=False)))
    elif kind == "native_number_decimal":
        # RDFLib retains a Python int here; pySHACL datatype validation rejects
        # it, although reparsing its serialization would create Decimal(24).
        graph.set((node, predicate, Literal(24, datatype=XSD.decimal)))
    return graph


def probe():
    results = []
    for domain in ("hvac", "ev"):
        namespace = "http://example.org/building#" if domain == "hvac" else ev.EV
        predicate = URIRef(namespace + ("operatorMaxSetpoint" if domain == "hvac" else "driverMaxPower"))
        shape_path = ROOT / ("shapes/invariants.ttl" if domain == "hvac" else "ev/shapes/invariants_ev.ttl")
        shape_graph = Graph().parse(shape_path, format="turtle")
        for kind in ("multiple", "missing_node", "missing_value", "wrong_datatype", "nonfinite", "native_number_decimal"):
            state = load_state("data/base_graph.ttl") if domain == "hvac" else ev.load_state(ev.BASE_GRAPH_PATH)
            malformed(state, URIRef(namespace + "Policy"), predicate, kind)
            if domain == "hvac":
                entries = {"engine_empty": lambda: run_engine(state_graph=state, events=[], save_trace_file=False),
                           "reference_empty": lambda: resolve_actions(state, []),
                           "incremental_empty": lambda: resolve_actions_incremental(state, [])}
                fast, reference = check_admissibility_incremental(state)[0], check_admissibility_shacl(state)[0]
                assert fast is False and reference is False
            else:
                entries = {"pipeline_empty": lambda: ev._pipeline([], ev.ROLE_RANK, state),
                           "reference_empty": lambda: ev.resolve_actions(state, [])}
                assert ev.check_admissibility_shacl(state)[0] is False
            entries["second_empty"] = lambda: second_resolve(state, [], {"domain": domain}, shape_graph)
            rejected = []
            for name, entry in entries.items():
                try:
                    entry()
                except ValueError:
                    rejected.append(name)
                else:
                    raise AssertionError(f"{domain}/{kind}/{name}: malformed policy accepted")
            results.append({"domain": domain, "case": kind, "rejected_at_initialization": rejected})
    _, _, _, successor, _ = run_engine(save_trace_file=False)
    state = load_state("data/base_graph.ttl")
    ev_state = ev.load_state(ev.BASE_GRAPH_PATH)
    for entry in (
        lambda: run_engine(state_graph=state, events=[], save_trace_file=False,
                           settings_override={"governance": {"conflict_policy": "unknown"}}),
        lambda: resolve_actions(state, [], settings={"governance": {"conflict_policy": "unknown"}}),
        lambda: resolve_actions_incremental(state, [], settings={"governance": {"conflict_policy": "unknown"}}),
        lambda: ev.resolve_actions(ev_state, [], conflict_policy="unknown"),
        lambda: second_resolve(state, [], {"conflict_policy": "unknown"}, Graph()),
    ):
        try:
            entry()
        except ValueError:
            pass
        else:
            raise AssertionError("Unknown conflict policy accepted")
    return {"invalid_inputs": results, "unknown_conflict_policy_refused": True,
            "default_digest": graph_digest(successor)}


def main():
    os.environ["ADMISSIBILITY_REGIME"] = "shacl"
    if "--child" in sys.argv:
        print(json.dumps(probe(), sort_keys=True))
        return
    local = probe()
    seeds = []
    for seed in range(16):
        env = dict(os.environ, PYTHONHASHSEED=str(seed), PYTHONWARNINGS="ignore")
        child = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--child"],
                               cwd=ROOT, env=env, text=True, capture_output=True, check=True)
        value = json.loads(child.stdout.strip().splitlines()[-1])
        assert value == local, f"Policy-profile divergence with hash seed {seed}"
        seeds.append({"seed": seed, "default_digest": value["default_digest"], "refusals_match": True})
    # Preserve the intentionally supported recovery from an invalid domain state.
    from experiment_invalid_start import run_scenario
    recovery = run_scenario("single_violation")
    assert recovery["recovered"] and recovery["accepted"] == 4
    result = {"environment": _environment(), "admissibility_regime": "shacl", **local,
              "hash_seeds": seeds, "invalid_domain_state_recovery_preserved": True, "overall_pass": True}
    Path(paths.hvac("experiment_policy_profile.json")).write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"Policy profile: {len(local['invalid_inputs'])} malformed inputs refused, all empty entry points")
    print("16 hash-seed processes agree; invalid-domain-state recovery preserved")


if __name__ == "__main__":
    main()
