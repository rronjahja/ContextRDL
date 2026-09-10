"""Boundary and generated-state checks with application-level expected outcomes.

Run from the repository root:
    python src/experiment_semantic_boundaries.py

Writes results/semantic_boundaries.json, including failures, before exiting.
--smoke selects a smaller development matrix. --check-only writes no report.
No expected outcome is obtained by asking either resolver for the answer.
Numeric oracles use Fraction; the EV outcome oracle uses explicit postconditions
for one charging point, not another implementation of the four-gate loop.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from decimal import Decimal, localcontext
from fractions import Fraction
import itertools
import json
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "ev"), str(ROOT / "src")]

from rdflib import Graph, Literal, URIRef
from rdflib.namespace import RDF, XSD
import experiment_ev as ev
from experiment_operational import event
import independent_resolver as second
import paths
from admissibility import check_admissibility
from dataset_builder import load_events, load_state
from engine import run_engine
from numeric_profile import NUMERIC_PROFILE, decimal_add, decimal_literal, decimal_value
from replay_full import replay_full
from resolver_incremental import resolve_actions_incremental
from trace import _environment, graph_digest

EV_CONFIG = {"domain": "ev", "schedule_key": ev.SCHEDULE_KEY,
             "active_roles": ev.ACTIVE_ROLES, "enforce_active_roles": True,
             "conflict_policy": ev.CONFLICT_POLICY}
FEEDER = URIRef(ev.EV + "Feeder1")
BUDGET = URIRef(ev.EV + "feederBudget")
BUILDING = "http://example.org/building#"


@contextmanager
def regime(name):
    previous = os.environ.get("ADMISSIBILITY_REGIME")
    os.environ["ADMISSIBILITY_REGIME"] = name
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("ADMISSIBILITY_REGIME", None)
        else:
            os.environ["ADMISSIBILITY_REGIME"] = previous


def require(checks, name, condition):
    checks[name] = bool(condition)


def rejected_initialization(call):
    try:
        call()
    except ValueError:
        return True
    return False


def decision_fields(decisions):
    return [{"aid": d["aid"], "accepted": d["accepted"],
             "reason": d["reason"].split(":", 1)[0],
             "post_graph_digest": d["post_graph_digest"]} for d in decisions]


def ev_run(name, state, events, ingestion_control=False):
    _, enabled, schedule = ev._pipeline(events, ev.ROLE_RANK, state)
    accepted, successor, decisions = ev.resolve_actions(state, schedule)
    alternate = second.resolve(state, list(reversed(enabled)), EV_CONFIG, ev._shapes_graph())
    checks = {
        "initial_graph_conformance": ev.check_admissibility_shacl(state)[0],
        "final_graph_conformance": ev.check_admissibility_shacl(successor)[0],
        "independent_schedule": alternate["schedule_aids"] == [a["aid"] for a in schedule],
        "independent_decisions": alternate["decisions"] == decision_fields(decisions),
        "independent_successor": alternate["successor_digest"] == graph_digest(successor),
    }
    # Check each accepted numeric command against the exact policy value. This
    # oracle does not import the production or second implementation's guard.
    policy_ok = True
    for action in accepted:
        if action["predicate"] != ev.CHARGING_POWER:
            continue
        proposed = Fraction(str(action["value"]))
        low = Fraction(str(state.value(ev.POLICY_NODE, ev.MIN_POWER)))
        high = Fraction(str(state.value(ev.POLICY_NODE,
                                  URIRef(ev.EV + action["role"] + "MaxPower"))))
        policy_ok &= low <= proposed <= high
    checks["accepted_policy_compliance"] = policy_ok
    trace_path = ev.record_trace(name, state, enabled, ev.ROLE_RANK, schedule,
                                 decisions, accepted, successor)
    replay = ev.replay_trace(trace_path)
    checks["recorded_action_replay"] = all(replay[k] for k in (
        "dependencies_match", "schedule_match", "decisions_match", "accepted_match", "digest_match"))
    # Trace payload is retained in the report; temporary trace files are removed.
    trace = json.loads(Path(trace_path).read_text(encoding="utf-8"))
    if ingestion_control:
        _, _, reordered = ev._pipeline(list(reversed(events)), ev.ROLE_RANK, state)
        _, other_graph, other_decisions = ev.resolve_actions(state, reordered)
        checks["ingestion_permutation"] = (
            [a["aid"] for a in reordered] == [a["aid"] for a in schedule]
            and graph_digest(other_graph) == graph_digest(successor)
            and ev._comparable(other_decisions) == ev._comparable(decisions))
    return successor, decisions, checks, trace


def malformed_feeders():
    variants = {
        "missing_budget": [],
        "multiple_budgets": [Literal("50", datatype=XSD.decimal), Literal("60", datatype=XSD.decimal)],
        "integer_datatype": [Literal(50)],
        "untyped_string": [Literal("50")],
        "nonfinite": [Literal("NaN", datatype=XSD.decimal, normalize=False)],
        "malformed": [Literal("fifty", datatype=XSD.decimal, normalize=False)],
        "negative": [Literal("-1", datatype=XSD.decimal)],
        "iri": [URIRef("urn:budget")],
    }
    rows = []
    for name, values in variants.items():
        state = ev._primed_state({})
        state.remove((FEEDER, BUDGET, None))
        for value in values:
            state.add((FEEDER, BUDGET, value))
        checks = {
            "shape_rejects": not ev.check_admissibility_shacl(state)[0],
            "empty_window_initialization_rejects": rejected_initialization(
                lambda: ev._pipeline([], ev.ROLE_RANK, state)),
            "nonempty_window_initialization_rejects": rejected_initialization(
                lambda: ev._pipeline(ev.load_events(ev.EVENTS_PATH), ev.ROLE_RANK, state)),
            "empty_resolver_initialization_rejects": rejected_initialization(
                lambda: ev.resolve_actions(state, [])),
            "independent_initialization_rejects": rejected_initialization(
                lambda: second.resolve(state, [], EV_CONFIG, ev._shapes_graph())),
        }
        rows.append({"name": name, "checks": checks})
    for name in ("missing_feeder_node", "missing_feeder_type", "missing_feeder_state"):
        state = ev._primed_state({})
        if name == "missing_feeder_node":
            state.remove((FEEDER, None, None))
        elif name == "missing_feeder_type":
            state.remove((FEEDER, RDF.type, None))
        else:
            state.remove((FEEDER, URIRef(ev.EV + "feederState"), None))
        rows.append({"name": name, "checks": {"shape_rejects": not ev.check_admissibility_shacl(state)[0]}})
    state = ev._primed_state({})
    state.set((FEEDER, BUDGET, Literal("0", datatype=XSD.decimal)))
    successor, decisions, checks, trace = ev_run("zero_budget", state,
        [event("zero-budget", "ChargeRequest", "driver", cp="CP1", power=1)])
    checks["request_rejected"] = len(decisions) == 1 and decisions[0]["reason"] == "inadmissible"
    rows.append({"name": "valid_zero_budget", "checks": checks, "trace": trace})
    return rows


def near(integer, digits):
    return [str(integer - 1) + "." + "9" * digits,
            str(integer), str(integer) + "." + "0" * (digits - 1) + "1"]


def numeric_boundaries(scratch, smoke):
    rows = []
    for domain, bound, digits, varying in itertools.product(
            ("hvac", "ev"), ("minimum", "maximum"), (18,) if smoke else (18, 35),
            ("bound", "command")):
        for index, nearby in enumerate(near(21, digits)):
            limit = nearby if varying == "bound" else "21"
            value = nearby if varying == "command" else "21"
            expected = Fraction(value) >= Fraction(limit) if bound == "minimum" else Fraction(value) <= Fraction(limit)
            reason = "admissible" if expected else ("policy_min_violation" if bound == "minimum" else "policy_role_cap_violation")
            name = f"{domain}_{bound}_{digits}_{varying}_{index}"
            if domain == "ev":
                state = ev._primed_state({})
                pred = "minPower" if bound == "minimum" else "driverMaxPower"
                state.set((ev.POLICY_NODE, URIRef(ev.EV + pred), Literal(limit, datatype=XSD.decimal)))
                successor, decisions, checks, trace = ev_run(name, state,
                    [event(name, "ChargeRequest", "driver", cp="CP1", power=value)])
                committed = Fraction(str(successor.value(URIRef(ev.EV + "CP1"), URIRef(ev.CHARGING_POWER))))
            else:
                state = load_state("data/base_graph.ttl")
                pred = "minSetpoint" if bound == "minimum" else "operatorMaxSetpoint"
                state.set((URIRef(BUILDING + "Policy"), URIRef(BUILDING + pred), Literal(limit, datatype=XSD.decimal)))
                events = [{"eid": name, "timestamp": "2026-03-14T09:00:00Z", "role": "operator",
                           "type": "PreheatRequest", "payload": {"zone": BUILDING + "ZoneB", "target": value}}]
                path = str(scratch / (name + ".json"))
                with regime("shacl"):
                    _, schedule, accepted, successor, trace = run_engine(state_graph=state, events=events, trace_path=path)
                decisions = trace["decisions"]
                independent = second.resolve(state, list(reversed(schedule)),
                    {"schedule_key": ev.SCHEDULE_KEY}, Graph().parse("shapes/invariants.ttl"))
                with regime("incremental"):
                    _, incremental, incremental_decisions = resolve_actions_incremental(state, schedule, record_digests=True)
                replay = replay_full(path)
                checks = {
                    "final_graph_conformance": check_admissibility(successor, "shapes/invariants.ttl")[0],
                    "independent_decisions": independent["decisions"] == decision_fields(decisions),
                    "independent_successor": independent["successor_digest"] == graph_digest(successor),
                    "incremental_successor": graph_digest(incremental) == graph_digest(successor),
                    "incremental_decisions": decision_fields(incremental_decisions) == decision_fields(decisions),
                    "pipeline_replay": replay["overall_pass"],
                }
                committed = Fraction(str(successor.value(URIRef(BUILDING + "ZoneB"), URIRef(BUILDING + "currentSetpoint"))))
            checks["oracle_decision"] = (len(decisions) == 1 and decisions[0]["accepted"] == expected
                                         and decisions[0]["reason"].split(":", 1)[0] == reason)
            checks["exact_committed_value_or_rollback"] = committed == (Fraction(value) if expected else
                Fraction(str(state.value(URIRef((ev.EV + "CP1") if domain == "ev" else (BUILDING + "ZoneB")),
                    URIRef(ev.CHARGING_POWER if domain == "ev" else BUILDING + "currentSetpoint")))))
            rows.append({"name": name, "command": value, "bound": limit, "expected_acceptance": expected,
                         "checks": checks, "trace": trace})
    return rows


def numeric_plumbing(scratch):
    checks = {}
    for precision in (2, 28):
        with localcontext() as context:
            context.prec = precision
            value = decimal_add("20.99999999999999999999999999999999999", "0.00000000000000000000000000000000001")
            checks[f"exact_addition_at_context_{precision}"] = Fraction(str(value)) == 21
    for name, value in (("nan", float("nan")), ("infinity", "Infinity"), ("boolean", True)):
        checks[name + "_refused"] = rejected_initialization(lambda: decimal_value(value))
    exact = "21.000000000000000001"
    for domain in ("hvac", "ev"):
        if domain == "hvac":
            text = '{"eid":"json-decimal","timestamp":"2026-03-14T09:00:00Z","role":"operator","type":"PreheatRequest","payload":{"zone":"' + BUILDING + 'ZoneB","target":' + exact + '}}'
            key = "target"
        else:
            text = '{"eid":"json-decimal","timestamp":"2026-03-14T09:00:00Z","role":"driver","type":"ChargeRequest","payload":{"cp":"' + ev.EV + 'CP1","power":' + exact + '}}'
            key = "power"
        path = scratch / (domain + "_decimal_events.jsonl")
        path.write_text(text + "\n", encoding="utf-8")
        loaded = load_events(str(path)) if domain == "hvac" else ev.load_events(str(path))
        checks[domain + "_json_preserves_value"] = Fraction(str(loaded[0]["payload"][key])) == Fraction(exact)
    # The helper above is also exercised through HVAC action construction and replay.
    state = load_state("data/base_graph.ttl")
    original = Fraction(str(state.value(URIRef(BUILDING + "ZoneB"), URIRef(BUILDING + "currentSetpoint"))))
    delta = "0.00000000000000000000000000000000001"
    events = [{"eid": "add", "timestamp": "2026-03-14T09:00:00Z", "type": "OccupantSetpointRequest",
               "role": "occupant", "payload": {"zone": BUILDING + "ZoneB", "delta": delta}}]
    trace_path = str(scratch / "addition.json")
    _, _, accepted, successor, _ = run_engine(state_graph=state, events=events, trace_path=trace_path)
    checks["constructed_sum_exact"] = len(accepted) == 1 and Fraction(str(accepted[0]["value"])) == original + Fraction(delta)
    checks["committed_sum_exact"] = Fraction(str(successor.value(URIRef(BUILDING + "ZoneB"), URIRef(BUILDING + "currentSetpoint")))) == original + Fraction(delta)
    checks["addition_pipeline_replay"] = replay_full(trace_path)["overall_pass"]
    return [{"name": "numeric_plumbing", "checks": checks}]


def aggregate_boundaries(smoke):
    rows = []
    for digits in ((18,) if smoke else (18, 35)):
        for index, value in enumerate(near(10, digits)):
            state = ev._primed_state({})
            events = [event("a", "ChargeRequest", "driver", cp="CP1", power=20),
                      event("b", "ChargeRequest", "driver", cp="CP2", power=20),
                      event("c", "ChargeRequest", "driver", cp="CP3", power=value)]
            requested = Fraction(40) + Fraction(value)
            expected = requested <= 50
            name = f"aggregate_{digits}_{index}"
            # Direct constraint check isolates the aggregate from scheduling.
            candidate = ev._primed_state({"CP1": 20, "CP2": 20, "CP3": value})
            checks = {}
            for precision in (2, 28):
                with localcontext() as context:
                    context.prec = precision
                    checks[f"sum_oracle_at_context_{precision}"] = ev.check_admissibility_shacl(candidate)[0] == expected
            successor, decisions, execution_checks, trace = ev_run(name, state, events)
            checks.update(execution_checks)
            checks["budget_postcondition"] = sum(Fraction(str(o)) for o in successor.objects(None, URIRef(ev.CHARGING_POWER))) <= 50
            cp3 = Fraction(str(successor.value(URIRef(ev.EV + "CP3"), URIRef(ev.CHARGING_POWER))))
            checks["exact_third_request_or_rollback"] = cp3 == (Fraction(value) if expected else 0)
            rows.append({"name": name, "requested_total": str(requested), "all_requests_fit": expected,
                         "checks": checks, "trace": trace})
    return rows


def matrix_oracle(connector, power, feeder, role, command, signal):
    """Postconditions for this bounded one-point application, without a resolver."""
    final_connector = "outOfService" if signal == "fault" else (
        "occupied" if signal == "plug" and connector == "free" else connector)
    final_feeder = "curtailed" if signal == "emergency" else feeder
    if signal == "fault":
        return 0, final_connector, final_feeder
    if signal == "emergency" and power > 5:
        return 5, final_connector, final_feeder
    # A higher-role command encounters the free connector before the driver
    # plug-in; the single pass does not reconsider that rejected command.
    ready_for_command = connector in ("occupied", "charging") or (
        signal == "plug" and connector == "free" and role == "driver")
    allowed = (command == 0 or ready_for_command) and (final_feeder != "curtailed" or command <= 5)
    return (command if allowed else power), final_connector, final_feeder


def generated_matrix(smoke):
    rows = []
    for feeder, connector, power, role, command, signal in itertools.product(
            ("normal",) if smoke else ("normal", "curtailed"),
            ("free", "occupied", "charging", "outOfService"),
            (0,) if smoke else (0, 5, 22), ("driver", "fleet", "grid"),
            (10,) if smoke else (0, 5, 10, 22), ("none", "plug", "fault", "emergency")):
        # Validity is decided from the fixture specification, not from SHACL.
        if power > 0 and connector not in ("occupied", "charging"):
            continue
        if feeder == "curtailed" and power > 5:
            continue
        name = f"matrix_{feeder}_{connector}_{power}_{role}_{command}_{signal}"
        state = ev._primed_state({"CP1": power}, {"CP1": connector})
        state.set((FEEDER, URIRef(ev.EV + "feederState"), Literal(feeder)))
        kind = {"driver": "ChargeRequest", "fleet": "FleetSchedule", "grid": "CapacitySignal"}[role]
        payload = {"cp": "CP1", "cap" if role == "grid" else "power": command}
        events = [event("command", kind, role, **payload)]
        if signal == "plug":
            events.append(event("signal", "PlugIn", "driver", cp="CP1"))
        elif signal == "fault":
            events.append(event("signal", "FaultDetected", "safety", cp="CP1"))
        elif signal == "emergency":
            events.append(event("signal", "GridEmergency", "grid", feeder="Feeder1"))
        expected = matrix_oracle(connector, power, feeder, role, command, signal)
        successor, decisions, checks, trace = ev_run(name, state, events, ingestion_control=True)
        observed = (Fraction(str(successor.value(URIRef(ev.EV + "CP1"), URIRef(ev.CHARGING_POWER)))),
                    str(successor.value(URIRef(ev.EV + "CP1"), URIRef(ev.EV + "connectorState"))),
                    str(successor.value(FEEDER, URIRef(ev.EV + "feederState"))))
        checks["application_postcondition"] = observed == expected
        rows.append({"name": name, "events": events, "expected": expected,
                     "observed": [str(observed[0]), observed[1], observed[2]],
                     "requested_power_achieved": observed[0] == command,
                     "checks": checks, "trace": trace})
        if len(rows) % 50 == 0:
            print(f"Checked {len(rows)} generated EV cases", flush=True)
        if not all(checks.values()):
            print("Failed postcondition: " + name, flush=True)
    # An emergency naming another feeder must not reduce this feeder's points.
    state = ev._primed_state({"CP1": 22})
    successor, decisions, checks, trace = ev_run("other_feeder", state,
        [event("other", "GridEmergency", "grid", feeder="OtherFeeder")])
    checks["other_feeder_has_no_effect"] = not decisions and graph_digest(successor) == graph_digest(state)
    rows.append({"name": "other_feeder", "checks": checks, "trace": trace})
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument("--output", type=Path, default=ROOT / "results" / "semantic_boundaries.json")
    args = parser.parse_args()
    if sys.flags.optimize:
        raise SystemExit("Run without -O so that all existing experiment assertions remain enabled")
    report = {"environment": _environment(), "numeric_profile": NUMERIC_PROFILE,
              "scope": "smoke" if args.smoke else "full_bounded_matrix", "groups": {}, "overall_pass": False}
    previous_traces = paths.EV_TRACES
    error = None
    try:
        with tempfile.TemporaryDirectory(prefix="contextrdl-boundaries-") as directory, regime("shacl"):
            scratch = Path(directory)
            paths.EV_TRACES = scratch / "ev_traces"
            groups = (("feeder_configuration", malformed_feeders),
                      ("numeric_plumbing", lambda: numeric_plumbing(scratch)),
                      ("numeric_boundaries", lambda: numeric_boundaries(scratch, args.smoke)),
                      ("aggregate_boundaries", lambda: aggregate_boundaries(args.smoke)),
                      ("generated_ev_states", lambda: generated_matrix(args.smoke)))
            for name, run in groups:
                print("Checking " + name, flush=True)
                report["groups"][name] = run()
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        report["error"] = error
    finally:
        paths.EV_TRACES = previous_traces
    failures = [f"{group}/{row['name']}/{check}" for group, rows in report["groups"].items()
                for row in rows for check, ok in row["checks"].items() if not ok]
    report["failures"] = failures
    report["overall_pass"] = error is None and not failures
    report["case_counts"] = {name: len(rows) for name, rows in report["groups"].items()}
    # Success here means agreement with the declared postconditions, not that
    # every power request succeeded. Report unmet requests separately.
    matrix = report["groups"].get("generated_ev_states", [])
    report["unmet_request_cases"] = [row["name"] for row in matrix if row.get("requested_power_achieved") is False]
    if not args.check_only:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        print("Report: " + str(args.output))
    if error or failures:
        raise SystemExit(error or "Failed checks: " + "; ".join(failures))
    print("Boundary checks passed for the selected scope.")


if __name__ == "__main__":
    main()
