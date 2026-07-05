"""
Second application scenario: EV-charging cluster.

This experiment exercises the *same* execution semantics on a different domain
with a qualitatively different (cross-target) constraint structure. It is
deliberately self-contained: it does NOT modify the HVAC pipeline. Instead it
reuses the domain-agnostic parts of that pipeline --

  * ``rule_engine.schedule_actions``  -- the deterministic scheduler, unchanged;
  * ``trace.graph_digest`` / ``graph_delta`` -- domain-agnostic serialization;

-- and supplies EV-specific data plumbing (dataset builder, action constructor,
apply_action) plus the reference pySHACL validator. The feeder-budget and
curtailment shapes are cross-target, so per the paper the reference SHACL
validator is used (the incremental zone-local validator does not apply here).

The resolution loop below is structurally identical to ``resolver.resolve_actions``:
first-writer-wins shadowing, then policy guard, then admissibility against the
candidate successor graph. The point of the scenario is that none of these
stages are HVAC-specific.

Workloads:
  * headline    : 3 simultaneous 22 kW charge requests on CP1/CP2/CP3.
                  Each admissible alone; each pair admissible (44<=50);
                  the triple inadmissible (66>50). Non-pairwise conflict.
  * governance  : a grid capacity signal competes with a fleet schedule on the
                  same charging point; precedence selects the committed value.

Outputs: ev/results/experiment_ev.json
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import statistics
import sys
import time
from copy import deepcopy
from datetime import timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from rdflib import Dataset, Graph, Literal, URIRef
from rdflib.namespace import RDF, XSD

# Reuse domain-agnostic machinery from the existing project.
HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "src"
sys.path.insert(0, str(SRC))

from rule_engine import schedule_actions  # noqa: E402  (domain-agnostic scheduler)
from trace import graph_digest, graph_delta  # noqa: E402  (domain-agnostic digests)

EV = "http://example.org/ev#"
URN_PROP_NS = "urn:prop:"
STATE_GRAPH_IRI = URIRef("urn:state")
WINDOW_ALIAS_IRI = URIRef("urn:window")

POLICY_NODE = URIRef(f"{EV}Policy")
CHARGING_POWER = f"{EV}chargingPower"
MIN_POWER = URIRef(f"{EV}minPower")
ROLE_MAX_PREDICATES = {
    "driver": URIRef(f"{EV}driverMaxPower"),
    "fleet": URIRef(f"{EV}fleetMaxPower"),
    "grid": URIRef(f"{EV}gridMaxPower"),
}

# EV payload typing for the dataset builder.
NUMERIC_PAYLOAD_KEYS = {"power", "cap"}
URI_PAYLOAD_KEYS = {"cp", "feeder"}
# EV predicate typing for graph mutation.
DECIMAL_PREDICATES = {CHARGING_POWER}

SHAPES_PATH = str(HERE / "shapes" / "invariants_ev.ttl")
BASE_GRAPH_PATH = str(HERE / "data" / "base_graph_ev.ttl")
RULES_PATH = str(HERE / "data" / "rules_ev.json")
EVENTS_PATH = str(HERE / "data" / "events_ev.jsonl")

# Default governance: grid > fleet > driver.
ROLE_RANK = {"grid": 0, "fleet": 1, "driver": 2}
SCHEDULE_KEY = ["roleRank", "priority", "tsKey", "rid", "bindKey", "aid"]


# ---------------------------------------------------------------------------
# Data plumbing (EV-aware versions of the HVAC helpers)
# ---------------------------------------------------------------------------

def prop_uri(name: str) -> URIRef:
    return URIRef(f"{URN_PROP_NS}{name}")


def _stable_json(data: Any) -> str:
    def _default(v: Any) -> Any:
        if isinstance(v, (set, tuple)):
            return list(v)
        return str(v)
    return json.dumps(data, sort_keys=True, separators=(",", ":"), default=_default)


def parse_ts_key(ts: str) -> int:
    from datetime import datetime
    text = ts.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.astimezone(timezone.utc).timestamp() * 1_000_000)


def load_state(path: str) -> Graph:
    g = Graph()
    g.parse(data=Path(path).read_text(encoding="utf-8"), format="turtle")
    return g


def load_events(path: str) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s:
            events.append(json.loads(s))
    return events


def load_rules(path: str) -> List[Dict[str, Any]]:
    return json.loads(Path(path).read_text(encoding="utf-8"))["rules"]


def _to_literal_for_payload(key: str, value: Any):
    if key in URI_PAYLOAD_KEYS:
        return URIRef(str(value))
    if key in NUMERIC_PAYLOAD_KEYS:
        return Literal(float(value), datatype=XSD.decimal)
    if isinstance(value, bool):
        return Literal(value, datatype=XSD.boolean)
    if isinstance(value, int):
        return Literal(value)
    if isinstance(value, float):
        return Literal(value, datatype=XSD.decimal)
    return Literal(str(value))


def build_dataset(state_graph: Graph, events: List[Dict[str, Any]]) -> Tuple[Dataset, Dict[str, Any]]:
    """All events share one window (single evaluation step), mirroring the HVAC default."""
    ds = Dataset()
    state = ds.graph(STATE_GRAPH_IRI)
    for t in state_graph:
        state.add(t)

    # Sort events by (timestamp, eid), as the HVAC builder does.
    ordered = sorted(events, key=lambda e: (e["timestamp"], e["eid"]))
    window_id = "ev:single-window"
    alias = ds.graph(WINDOW_ALIAS_IRI)
    named = ds.graph(URIRef(f"urn:window:{window_id}"))

    for ordinal, event in enumerate(ordered):
        for g in (alias, named):
            uri = URIRef(f"urn:event:{event['eid']}")
            g.add((uri, prop_uri("eid"), Literal(str(event["eid"]))))
            g.add((uri, prop_uri("timestamp"), Literal(str(event["timestamp"]))))
            g.add((uri, prop_uri("type"), Literal(str(event["type"]))))
            g.add((uri, prop_uri("role"), Literal(str(event["role"]))))
            g.add((uri, prop_uri("order"), Literal(ordinal)))
            for key, value in event.get("payload", {}).items():
                g.add((uri, prop_uri(key), _to_literal_for_payload(key, value)))

    meta = {"window_id": window_id, "selected_event_ids": [e["eid"] for e in ordered]}
    return ds, meta


def _row_bindings(row: Any) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for k, v in row.asdict().items():
        if isinstance(v, Literal):
            out[k] = v.toPython()
        elif isinstance(v, URIRef):
            out[k] = str(v)
        else:
            out[k] = str(v)
    return out


def build_action_value(rule: Mapping[str, Any], bindings: Mapping[str, Any]) -> Any:
    ve = rule["insert_template"]["value_expr"]
    kind = ve.get("kind")
    if kind == "copy":
        return bindings[ve["var"]]
    if kind == "literal":
        return ve["value"]
    if kind == "numeric_add":
        return float(bindings[ve["left"]]) + float(bindings[ve["right"]])
    if kind == "numeric_add_constant":
        return float(bindings[ve["var"]]) + float(ve["constant"])
    raise ValueError(f"Unsupported value_expr kind: {kind}")


def _subject_var(rule: Mapping[str, Any]) -> str:
    """EV rules target either a charging point (?cp) or the feeder (?feeder)."""
    return "feeder" if rule["insert_template"]["predicate"] == f"{EV}feederState" else "cp"


def create_action(
    rule: Mapping[str, Any],
    row: Any,
    role_rank: Mapping[str, int],
    window_id: str,
    event_role: Optional[str],
) -> Dict[str, Any]:
    bindings = _row_bindings(row)
    predicate = rule["insert_template"]["predicate"]
    rid = rule["rid"]
    role = rule["issuing_role"]

    subj_var = _subject_var(rule)
    subject = str(bindings[subj_var])

    event_uri = str(bindings.get("e", ""))
    event_id = str(bindings.get("eid") or event_uri.rsplit(":", 1)[-1])
    event_ts = str(bindings.get("ts", ""))

    bind_key_payload = {k: bindings[k] for k in sorted(bindings) if k != "e"}
    bind_key = _stable_json(bind_key_payload)

    aid_payload = {
        "rid": rid, "eid": event_id, "window_id": window_id,
        "predicate": predicate, "bindings": bind_key_payload,
    }
    aid = hashlib.sha256(_stable_json(aid_payload).encode("utf-8")).hexdigest()

    return {
        "aid": aid,
        "rid": rid,
        "event_id": event_id,
        "event_role": event_role,
        "window_id": window_id,
        # "zone" key reused as the generic subject so apply_action stays uniform
        "zone": subject,
        "predicate": predicate,
        "target_key": f"{subject}|{predicate}",
        "value": build_action_value(rule, bindings),
        "priority": int(rule["priority"]),
        "role": role,
        "roleRank": int(role_rank.get(role, 999)),
        "tsKey": parse_ts_key(event_ts) if event_ts else 0,
        "bindKey": bind_key,
        "bindings": bind_key_payload,
    }


def evaluate_rules(
    dataset: Dataset,
    rules: List[Dict[str, Any]],
    role_rank: Mapping[str, int],
    window_id: str,
) -> List[Dict[str, Any]]:
    actions: List[Dict[str, Any]] = []
    seen = set()
    alias = dataset.graph(WINDOW_ALIAS_IRI)
    for rule in rules:
        rule_role = rule["issuing_role"]
        for row in dataset.query(rule["condition_select"]):
            bindings = _row_bindings(row)
            event_uri = str(bindings.get("e", ""))
            event_role = None
            if event_uri:
                rv = alias.value(URIRef(event_uri), prop_uri("role"))
                if rv is not None:
                    event_role = str(rv.toPython()) if isinstance(rv, Literal) else str(rv)
            if event_role is not None and event_role != rule_role:
                continue
            action = create_action(rule, row, role_rank, window_id, event_role)
            if action["aid"] in seen:
                continue
            seen.add(action["aid"])
            actions.append(action)
    return actions


def make_literal(predicate: str, value: Any) -> Literal:
    if predicate in DECIMAL_PREDICATES:
        return Literal(float(value), datatype=XSD.decimal)
    return Literal(str(value))


def apply_action(graph: Graph, action: Mapping[str, Any]) -> Graph:
    new_graph = Graph()
    for t in graph:
        new_graph.add(t)
    subj = URIRef(action["zone"])
    pred = URIRef(action["predicate"])
    for t in list(new_graph.triples((subj, pred, None))):
        new_graph.remove(t)
    new_graph.add((subj, pred, make_literal(action["predicate"], action["value"])))
    return new_graph


# ---------------------------------------------------------------------------
# Admissibility (reference pySHACL only; shapes are cross-target)
# ---------------------------------------------------------------------------

_SHAPES_CACHE: Optional[Graph] = None


def _shapes_graph() -> Graph:
    global _SHAPES_CACHE
    if _SHAPES_CACHE is None:
        g = Graph()
        g.parse(data=Path(SHAPES_PATH).read_text(encoding="utf-8"), format="turtle")
        _SHAPES_CACHE = g
    return _SHAPES_CACHE


def check_admissibility_shacl(graph: Graph) -> Tuple[bool, str]:
    from pyshacl import validate
    conforms, _g, text = validate(
        data_graph=graph, shacl_graph=_shapes_graph(),
        inference=None, advanced=True, debug=False,
    )
    return bool(conforms), text


def check_policy_guard(graph: Graph, action: Mapping[str, Any]) -> Tuple[bool, str]:
    if action.get("predicate") != CHARGING_POWER:
        return True, "policy_guard_not_applicable"
    role = str(action.get("role"))
    max_pred = ROLE_MAX_PREDICATES.get(role)
    if max_pred is None:
        return True, "policy_guard_not_applicable"
    proposed = float(action["value"])
    for obj in graph.objects(POLICY_NODE, MIN_POWER):
        if proposed < float(obj.toPython()):
            return False, f"policy_min_power_violation:{proposed} < {float(obj.toPython())}"
    for obj in graph.objects(POLICY_NODE, max_pred):
        if proposed > float(obj.toPython()):
            return False, f"policy_role_cap_violation:{role}:{proposed} > {float(obj.toPython())}"
    return True, "policy_guard_passed"


# ---------------------------------------------------------------------------
# Resolution (structurally identical to resolver.resolve_actions)
# ---------------------------------------------------------------------------

def resolve_actions(
    graph: Graph,
    schedule: List[Mapping[str, Any]],
    conflict_policy: str = "first_writer_wins",
) -> Tuple[List[Dict[str, Any]], Graph, List[Dict[str, Any]]]:
    accepted: List[Dict[str, Any]] = []
    decisions: List[Dict[str, Any]] = []
    current = Graph()
    for t in graph:
        current.add(t)
    accepted_targets: Dict[str, Mapping[str, Any]] = {}

    for index, action in enumerate(schedule):
        target_key = str(action["target_key"])
        pre_digest = graph_digest(current)
        decision: Dict[str, Any] = {
            "schedule_index": index,
            "aid": action["aid"],
            "rid": action["rid"],
            "role": action.get("role"),
            "target_key": target_key,
            "target": {"subject": action["zone"], "predicate": action["predicate"], "value": action["value"]},
            "pre_graph_digest": pre_digest,
        }

        if conflict_policy == "first_writer_wins" and target_key in accepted_targets:
            w = accepted_targets[target_key]
            decision.update({"accepted": False, "reason": "shadowed_by_prior_accepted_action",
                             "blocked_by_aid": w["aid"], "blocked_by_rid": w["rid"],
                             "post_graph_digest": pre_digest})
            decisions.append(decision)
            continue

        ok, preason = check_policy_guard(current, action)
        if not ok:
            decision.update({"accepted": False, "reason": preason, "policy_reason": preason,
                             "post_graph_digest": pre_digest})
            decisions.append(decision)
            continue

        candidate = apply_action(current, action)
        cand_digest = graph_digest(candidate)
        conforms, report = check_admissibility_shacl(candidate)
        if conforms:
            removed, inserted = graph_delta(current, candidate)
            current = candidate
            accepted_targets[target_key] = action
            accepted.append(dict(action))
            decision.update({"accepted": True, "reason": "admissible", "policy_reason": preason,
                             "candidate_graph_digest": cand_digest, "post_graph_digest": cand_digest,
                             "removed_triples": removed, "inserted_triples": inserted})
        else:
            decision.update({"accepted": False, "reason": "inadmissible", "policy_reason": preason,
                             "candidate_graph_digest": cand_digest, "post_graph_digest": pre_digest})
        decisions.append(decision)

    return accepted, current, decisions


# ---------------------------------------------------------------------------
# Workloads
# ---------------------------------------------------------------------------

def _pipeline(events: List[Dict[str, Any]], role_rank: Mapping[str, int],
              state: Optional[Graph] = None):
    if state is None:
        state = load_state(BASE_GRAPH_PATH)
    rules = load_rules(RULES_PATH)
    dataset, meta = build_dataset(state, events)
    enabled = evaluate_rules(dataset, rules, role_rank, meta["window_id"])
    schedule = schedule_actions(enabled, settings={"schedule_key": SCHEDULE_KEY})
    return state, enabled, schedule


def _primed_state(charging: Mapping[str, float]) -> Graph:
    """Base EV graph with some charging points already drawing power."""
    g = load_state(BASE_GRAPH_PATH)
    for cp, val in charging.items():
        s = URIRef(f"{EV}{cp}")
        p = URIRef(CHARGING_POWER)
        for t in list(g.triples((s, p, None))):
            g.remove(t)
        g.add((s, p, Literal(float(val), datatype=XSD.decimal)))
    return g


def run_emergency() -> Dict[str, Any]:
    """
    Grid-emergency window. State is primed with CP1=CP2=22 kW. A GridEmergency
    fires q5 (reduce every CP to 5 kW, priority 0) and q4 (assert curtailment,
    priority 1). The lower priority number on q5 means load reductions are
    committed BEFORE curtailment is asserted, so the cross-target
    curtailment-consistency shape is satisfied along the committed path.
    """
    state = _primed_state({"CP1": 22, "CP2": 22})
    events = [{"eid": "em-001", "timestamp": "2026-03-14T09:00:00Z", "type": "GridEmergency",
               "role": "grid", "payload": {"feeder": f"{EV}Feeder1"}}]
    _state, enabled, schedule = _pipeline(events, ROLE_RANK, state=state)
    accepted, successor, decisions = resolve_actions(state, schedule)

    powers, feeder_state = {}, None
    for s, p, o in successor:
        if str(p) == CHARGING_POWER:
            powers[str(s).split("#")[-1]] = float(o.toPython())
        if str(p) == f"{EV}feederState":
            feeder_state = str(o.toPython())
    ok, _ = check_admissibility_shacl(successor)
    return {
        "enabled_count": len(enabled),
        "schedule": [(a["rid"], a["zone"].split("#")[-1], a["value"]) for a in schedule],
        "accepted_rids": [a["rid"] for a in accepted],
        "decisions": [{"rid": d["rid"], "subject": d["target"]["subject"].split("#")[-1],
                       "value": d["target"]["value"], "accepted": d["accepted"], "reason": d["reason"]}
                      for d in decisions],
        "committed_powers": powers,
        "committed_feeder_state": feeder_state,
        "successor_digest": graph_digest(successor),
        "final_admissible": ok,
    }


def run_headline() -> Dict[str, Any]:
    events = load_events(EVENTS_PATH)
    state, enabled, schedule = _pipeline(events, ROLE_RANK)
    accepted, successor, decisions = resolve_actions(state, schedule)

    committed_powers = {}
    for s, p, o in successor:
        if str(p) == CHARGING_POWER:
            committed_powers[str(s).split("#")[-1]] = float(o.toPython())

    final_ok, _ = check_admissibility_shacl(successor)
    return {
        "enabled_count": len(enabled),
        "schedule_rids": [a["rid"] for a in schedule],
        "accepted_rids": [a["rid"] for a in accepted],
        "decisions": [{"rid": d["rid"], "subject": d["target"]["subject"].split("#")[-1],
                       "value": d["target"]["value"], "accepted": d["accepted"], "reason": d["reason"]}
                      for d in decisions],
        "committed_powers": committed_powers,
        "successor_digest": graph_digest(successor),
        "final_admissible": final_ok,
    }


def run_headline_30_vs_random(runs: int = 30) -> Dict[str, Any]:
    events = load_events(EVENTS_PATH)
    state, enabled, schedule = _pipeline(events, ROLE_RANK)

    ours_digests, ours_admissible, ours_times = set(), 0, []
    for _ in range(runs):
        t0 = time.perf_counter()
        _acc, succ, _dec = resolve_actions(state, schedule)
        ours_times.append(time.perf_counter() - t0)
        ours_digests.add(graph_digest(succ))
        ok, _ = check_admissibility_shacl(succ)
        if ok:
            ours_admissible += 1

    # Random-order baseline: apply all enabled actions in random order, no gates,
    # validate the final graph once. Last-writer-wins per target.
    rng = random.Random(20260314)
    rand_digests, rand_admissible = set(), 0
    for _ in range(runs):
        order = list(enabled)
        rng.shuffle(order)
        working = Graph()
        for t in state:
            working.add(t)
        for a in order:
            working = apply_action(working, a)
        rand_digests.add(graph_digest(working))
        ok, _ = check_admissibility_shacl(working)
        if ok:
            rand_admissible += 1

    # Deterministic-order baseline: fixed schedule, no gates, validate once.
    det_digests, det_admissible = set(), 0
    for _ in range(runs):
        working = Graph()
        for t in state:
            working.add(t)
        for a in schedule:
            working = apply_action(working, a)
        det_digests.add(graph_digest(working))
        ok, _ = check_admissibility_shacl(working)
        if ok:
            det_admissible += 1

    return {
        "runs": runs,
        "ours_unique_states": len(ours_digests),
        "ours_admissible_pct": 100.0 * ours_admissible / runs,
        "ours_mean_runtime_ms": 1000.0 * statistics.mean(ours_times),
        "ours_sd_runtime_ms": 1000.0 * (statistics.stdev(ours_times) if len(ours_times) > 1 else 0.0),
        "random_unique_states": len(rand_digests),
        "random_admissible_pct": 100.0 * rand_admissible / runs,
        "deterministic_unique_states": len(det_digests),
        "deterministic_admissible_pct": 100.0 * det_admissible / runs,
    }


def governance_events(target_cp: str = "http://example.org/ev#CP1") -> List[Dict[str, Any]]:
    """A grid capacity signal (cap 10) competes with a fleet schedule (22) on the same CP."""
    ts = "2026-03-14T09:00:00Z"
    return [
        {"eid": "gov-fleet-001", "timestamp": ts, "type": "FleetSchedule", "role": "fleet",
         "payload": {"cp": target_cp, "power": 22}},
        {"eid": "gov-grid-001", "timestamp": ts, "type": "CapacitySignal", "role": "grid",
         "payload": {"cp": target_cp, "cap": 10}},
    ]


def run_governance() -> Dict[str, Any]:
    events = governance_events()
    out = {}
    for label, rank in (("grid_over_fleet", {"grid": 0, "fleet": 1, "driver": 2}),
                        ("fleet_over_grid", {"fleet": 0, "grid": 1, "driver": 2})):
        state, enabled, schedule = _pipeline(events, rank)
        accepted, successor, decisions = resolve_actions(state, schedule)
        committed = None
        for s, p, o in successor:
            if str(p) == CHARGING_POWER and str(s).endswith("CP1"):
                committed = float(o.toPython())
        ok, _ = check_admissibility_shacl(successor)
        out[label] = {
            "schedule_rids": [a["rid"] for a in schedule],
            "accepted_rids": [a["rid"] for a in accepted],
            "committed_power_CP1": committed,
            "final_admissible": ok,
            "decisions": [{"rid": d["rid"], "value": d["target"]["value"],
                           "accepted": d["accepted"], "reason": d["reason"]} for d in decisions],
        }
    return out


def main():
    out_dir = HERE / "results"
    out_dir.mkdir(exist_ok=True)

    headline = run_headline()
    stress = run_headline_30_vs_random()
    governance = run_governance()
    emergency = run_emergency()

    summary = {"headline": headline, "headline_30_trials": stress,
               "governance": governance, "emergency": emergency}
    with open(out_dir / "experiment_ev.json", "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2)

    print("== EV headline window (3x 22kW on CP1/CP2/CP3) ==")
    print("  enabled:", headline["enabled_count"], "| schedule:", headline["schedule_rids"])
    for d in headline["decisions"]:
        print(f"    {d['rid']:4s} {d['subject']:5s} value={d['value']:<6} -> {d['accepted']!s:5s} ({d['reason']})")
    print("  committed powers:", headline["committed_powers"])
    print("  final admissible:", headline["final_admissible"])
    print("  successor digest:", headline["successor_digest"][:16], "...")

    print("\n== EV headline, 30 trials ==")
    print(f"  ours:          {stress['ours_unique_states']} state(s), "
          f"{stress['ours_admissible_pct']:.1f}% admissible, "
          f"{stress['ours_mean_runtime_ms']:.2f}+-{stress['ours_sd_runtime_ms']:.2f} ms")
    print(f"  deterministic: {stress['deterministic_unique_states']} state(s), "
          f"{stress['deterministic_admissible_pct']:.1f}% admissible")
    print(f"  random:        {stress['random_unique_states']} state(s), "
          f"{stress['random_admissible_pct']:.1f}% admissible")

    print("\n== EV governance (grid capacity 10 vs fleet schedule 22 on CP1) ==")
    for label, g in governance.items():
        print(f"  {label}: committed CP1 = {g['committed_power_CP1']} kW, "
              f"accepted={g['accepted_rids']}, admissible={g['final_admissible']}")

    print("\n== EV grid-emergency window (primed CP1=CP2=22 kW) ==")
    print("  schedule:", [(r, s, v) for r, s, v in emergency["schedule"]])
    for d in emergency["decisions"]:
        print(f"    {d['rid']:4s} {d['subject']:8s} val={d['value']:<10} -> {d['accepted']!s:5s} ({d['reason']})")
    print("  committed powers:", emergency["committed_powers"])
    print("  committed feeder state:", emergency["committed_feeder_state"])
    print("  final admissible:", emergency["final_admissible"])

    print("\nWrote", out_dir / "experiment_ev.json")


if __name__ == "__main__":
    main()
