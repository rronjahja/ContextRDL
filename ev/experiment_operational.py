"""EV requested outcomes, controlled comparisons and order-only ablation.

Run from the repository root: python ev/experiment_operational.py
Every fixture asserts its initial state, requested outcome, full replay and
agreement with the separate scheduling/resolution implementation.
"""
from __future__ import annotations

import itertools
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path[:0] = [str(HERE), str(ROOT / "src")]
import experiment_ev as ev
import independent_resolver as second
import paths
from rdflib import Graph, Literal, URIRef
from rdflib.namespace import XSD
from trace import _environment, graph_digest

STAMP = "2026-03-14T09:00:00Z"


def event(eid, kind, role, **payload):
    return {"eid": eid, "timestamp": STAMP, "type": kind, "role": role,
            "payload": {key: ev.EV + value if key in {"cp", "feeder"} else value
                        for key, value in payload.items()}}


def case_specs():
    emergency = event("em-1", "GridEmergency", "grid", feeder="Feeder1")
    fault = event("fault-1", "FaultDetected", "safety", cp="CP1")
    driver = event("driver-1", "ChargeRequest", "driver", cp="CP3", power=22)
    cases = []
    def add(name, events, power=None, connectors=None, **expect):
        cases.append(dict(name=name, events=events, power=power or {},
                          connectors=connectors or {}, expect=expect))
    add("emergency_alone", [emergency], {"CP1": 22, "CP2": 22}, emergency=True,
        powers={"CP1": 5, "CP2": 5, "CP3": 0, "CP4": 0})
    for name, cp, initial in (("charging", "CP1", 22), ("idle", "CP3", 0), ("at_limit", "CP3", 5)):
        power = {"CP1": 22, "CP2": 22, cp: initial}
        add("emergency_capacity_" + name,
            [emergency, event("cap-1", "CapacitySignal", "grid", cp=cp, cap=10)], power,
            emergency=True)
    # The requested 22 kW fits the normal budget and CP3 is already connected.
    add("driver_without_emergency", [driver], {"CP1": 10, "CP2": 10},
        {"CP3": "occupied"}, powers={"CP3": 22}, accepted_rules=["q1"])
    add("driver_with_emergency", [emergency, driver], {"CP1": 10, "CP2": 10},
        {"CP3": "occupied"}, emergency=True, powers={"CP3": 0}, rejected={"q1": "inadmissible"})
    add("fault_charging", [fault], {"CP1": 22}, {"CP1": "occupied"}, fault=True)
    add("fault_charge_free", [fault, event("charge-1", "ChargeRequest", "driver", cp="CP1", power=22)],
        connectors={"CP1": "free"}, fault=True)
    for name, other in (("capacity", event("cap-1", "CapacitySignal", "grid", cp="CP1", cap=10)),
                        ("emergency", emergency),
                        ("fleet", event("fleet-1", "FleetSchedule", "fleet", cp="CP1", power=22)),
                        ("departure", event("depart-1", "DepartureSoon", "fleet", cp="CP1"))):
        add("fault_with_" + name, [fault, other], {"CP1": 22}, {"CP1": "occupied"}, fault=True,
            **({"emergency": True} if name == "emergency" else {}))
    plug = event("plug-1", "PlugIn", "driver", cp="CP1")
    charge = event("charge-1", "ChargeRequest", "driver", cp="CP1", power=22)
    add("plug_and_charge", [plug, charge], connectors={"CP1": "free"},
        powers={"CP1": 22}, connector_states={"CP1": "occupied"}, accepted_rules=["q6", "q1"])
    add("plug_on_faulted", [plug, charge], connectors={"CP1": "outOfService"}, fault=True,
        rejected={"q1": "inadmissible"})
    add("departure", [event("depart-1", "DepartureSoon", "fleet", cp="CP1")],
        powers={"CP1": 22}, accepted_rules=["q7"])
    add("policy_role_cap", [charge], powers={"CP1": 0}, rejected={"q1": "policy_role_cap_violation"})
    cases[-1]["policy"] = {"driverMaxPower": 11}
    add("policy_minimum", [event("negative-1", "ChargeRequest", "driver", cp="CP1", power=-1)],
        powers={"CP1": 0}, rejected={"q1": "policy_min_violation"})
    add("inactive_driver", [charge], powers={"CP1": 0}, rejected={"q1": "inactive_role"})
    cases[-1]["active_roles"] = [role for role in ev.ACTIVE_ROLES if role != "driver"]
    return cases


def run_case(spec, record=True):
    state = ev._primed_state(spec["power"], spec["connectors"])
    for name, value in spec.get("policy", {}).items():
        state.set((ev.POLICY_NODE, URIRef(ev.EV + name), Literal(str(value), datatype=XSD.decimal)))
    assert ev.check_admissibility_shacl(state)[0], f"Invalid initial fixture: {spec['name']}"
    state, enabled, schedule = ev._pipeline(spec["events"], ev.ROLE_RANK, state)
    roles = spec.get("active_roles", ev.ACTIVE_ROLES)
    accepted, successor, decisions = ev.resolve_actions(state, schedule, active_roles=roles)
    powers = {str(s).split("#")[-1]: float(o.toPython()) for s, p, o in successor if str(p) == ev.CHARGING_POWER}
    connectors = {str(s).split("#")[-1]: str(o) for s, p, o in successor if str(p) == ev.EV + "connectorState"}
    feeder = str(successor.value(URIRef(ev.EV + "Feeder1"), URIRef(ev.EV + "feederState")))
    expected = spec["expect"]
    checks = {"initial_admissible": True, "final_admissible": ev.check_admissibility_shacl(successor)[0]}
    if expected.get("emergency"):
        checks["emergency_response"] = feeder == "curtailed" and all(value <= 5 for value in powers.values())
    if expected.get("fault"):
        checks["fault_response"] = connectors["CP1"] == "outOfService" and powers["CP1"] == 0
    for cp, value in expected.get("powers", {}).items():
        checks["power_" + cp] = powers.get(cp) == value
    for cp, value in expected.get("connector_states", {}).items():
        checks["connector_" + cp] = connectors.get(cp) == value
    if "accepted_rules" in expected:
        checks["accepted_rules"] = [a["rid"] for a in accepted] == expected["accepted_rules"]
    for rid, reason in expected.get("rejected", {}).items():
        checks["rejection_" + rid] = any(d["rid"] == rid and not d["accepted"]
                                         and d["reason"].split(":", 1)[0] == reason for d in decisions)
    independent = second.resolve(state, list(reversed(enabled)),
        {"domain": "ev", "schedule_key": ev.SCHEDULE_KEY, "active_roles": roles,
         "enforce_active_roles": True, "conflict_policy": ev.CONFLICT_POLICY}, ev._shapes_graph())
    checks["second_schedule"] = independent["schedule_aids"] == [a["aid"] for a in schedule]
    checks["second_digest"] = independent["successor_digest"] == graph_digest(successor)
    checks["second_decisions"] = independent["decisions"] == [
        {"aid": d["aid"], "accepted": d["accepted"], "reason": d["reason"].split(":", 1)[0],
         "post_graph_digest": d["post_graph_digest"]} for d in decisions]
    trace_path = None
    if record:
        trace_path = ev.record_trace("operational_" + spec["name"], state, enabled, ev.ROLE_RANK,
                                    schedule, decisions, accepted, successor, active_roles=roles)
        replay = ev.replay_trace(trace_path)
        checks["replay"] = all(replay[name] for name in ("dependencies_match", "schedule_match",
                            "decisions_match", "accepted_match", "digest_match"))
    assert all(checks.values()), f"{spec['name']}: {[key for key, value in checks.items() if not value]}"
    return {"name": spec["name"], "checks": checks, "powers": dict(sorted(powers.items())),
            "connectors": dict(sorted(connectors.items())), "feeder": feeder,
            "enabled_rules": [a["rid"] for a in enabled], "accepted_rules": [a["rid"] for a in accepted],
            "decisions": ev._comparable(decisions), "successor_digest": graph_digest(successor),
            "trace_path": paths.relative(trace_path) if trace_path else None}


def order_control():
    state, enabled, schedule = ev._pipeline(ev.load_events(ev.EVENTS_PATH), ev.ROLE_RANK)
    assert ev.check_admissibility_shacl(state)[0]
    subsets = []
    for size in (1, 2, 3):
        for subset in itertools.combinations(schedule, size):
            candidate = state
            for action in subset:
                candidate = ev.apply_action(candidate, action)
            valid = ev.check_admissibility_shacl(candidate)[0]
            assert valid == (size < 3)
            subsets.append({"subjects": [a["zone"] for a in subset], "admissible": valid})
    rows = []
    for order in itertools.permutations(schedule):
        accepted, successor, decisions = ev.resolve_actions(state, list(order))
        assert len(accepted) == 2 and ev.check_admissibility_shacl(successor)[0]
        rows.append({"order": [a["aid"] for a in order],
                     "accepted_subjects": [a["zone"] for a in accepted],
                     "successor_digest": graph_digest(successor), "final_admissible": True,
                     "decisions": ev._comparable(decisions)})
    unique = len({row["successor_digest"] for row in rows})
    assert unique == 3
    return {"gates": "all four unchanged; schedule order is the only perturbation",
            "subsets": subsets, "permutations": rows, "unique_states": unique,
            "admissible_permutations": len(rows)}


def main():
    cases = [run_case(spec) for spec in case_specs()]
    order = order_control()
    covered = set().union(*(set(row["enabled_rules"]) for row in cases))
    # q2 is checked here as well as in the legacy governance comparison.
    for row in ev.run_governance().values():
        assert row["final_admissible"]
        covered.update(row["schedule_rids"])
    declared = {rule["rid"] for rule in ev.load_rules(ev.RULES_PATH)}
    assert covered == declared
    result = {"environment": _environment(), "admissibility_regime": "shacl", "cases": cases,
              "order_control": order, "rule_coverage": sorted(covered), "overall_pass": True}
    Path(paths.ev("experiment_operational.json")).write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"EV operational outcomes: {len(cases)}/{len(cases)}; second implementation and replay agree")
    print(f"EV order control: six admissible permutations, {order['unique_states']} successor states")


if __name__ == "__main__":
    main()
