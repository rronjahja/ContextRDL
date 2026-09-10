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

The resolution loop below re-instantiates the four gates of Definition 9 in
the normative order of the execution configuration: (i) role filter,
(ii) policy guard, (iii) first-writer-wins conflict gate, (iv) admissibility
against the candidate successor graph (reference pySHACL validator, because the
EV shapes are cross-target). The scheduler and the digest routine are the
shared, domain-agnostic code; the action constructor, the graph mutation and
the policy guard are instantiated for the EV vocabulary. None of the stages is
HVAC-specific.

Every window is recorded as a trace (input graph snapshot, constructed action
instances, configuration, schedule, decisions, accepted set, successor digest)
under results/ev/traces/trace_ev_<window>.json, replayed from that file, and
re-executed in a separate interpreter process with a different hash seed.

Workloads:
  * headline    : 3 simultaneous 22 kW charge requests on CP1/CP2/CP3.
                  Each admissible alone; each pair admissible (44<=50);
                  the triple inadmissible (66>50). Non-pairwise conflict.
  * governance  : a grid capacity signal competes with a fleet schedule on the
                  same charging point; precedence selects the committed value.

Outputs: results/ev/experiment_ev.json and results/ev/traces/trace_ev_*.json
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
from decimal import localcontext, InvalidOperation
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from rdflib import Dataset, Graph, Literal, URIRef
from rdflib.namespace import RDF, XSD

# Reuse domain-agnostic machinery from the existing project.
HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "src"
sys.path.insert(0, str(SRC))

import paths  # noqa: E402  (results layout)
from rule_engine import BINDING_PROFILE, canonical_binding, schedule_actions  # noqa: E402  (shared scheduler and identity)
from rule_loader import validate_rules  # noqa: E402
from policy_profile import policy_literal, validate_policy_profile, validate_conflict_policy, PolicyProfileError
from numeric_profile import (NUMERIC_PROFILE, decimal_value, decimal_literal, decimal_add,
                             validation_decimal_context)
from trace import canonical_triple_lines, graph_digest, graph_delta  # noqa: E402  (domain-agnostic digests)

EV = "http://example.org/ev#"
URN_PROP_NS = "urn:prop:"
STATE_GRAPH_IRI = URIRef("urn:state")
WINDOW_ALIAS_IRI = URIRef("urn:window")

POLICY_NODE = URIRef(f"{EV}Policy")
CHARGING_POWER = f"{EV}chargingPower"
MIN_POWER = URIRef(f"{EV}minPower")
ROLE_MAX_PREDICATES = {
    "safety": URIRef(f"{EV}safetyMaxPower"),
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

# Fault shutdown precedes all grid and charging commands. FaultDetected events
# are issued by the safety role; event roles are trusted upstream inputs.
ROLE_RANK = {"safety": 0, "grid": 1, "fleet": 2, "driver": 3}
ACTIVE_ROLES = ["safety", "grid", "fleet", "driver"]
SCHEDULE_KEY = ["roleRank", "priority", "tsKey", "rid", "bindKey", "aid"]
CONFLICT_POLICY = "first_writer_wins"


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
            events.append(json.loads(s, parse_float=str))
    return events


def load_rules(path: str) -> List[Dict[str, Any]]:
    return validate_rules(json.loads(Path(path).read_text(encoding="utf-8"), parse_float=str)["rules"])


def _to_literal_for_payload(key: str, value: Any):
    if key in URI_PAYLOAD_KEYS:
        return URIRef(str(value))
    if key in NUMERIC_PAYLOAD_KEYS:
        return decimal_literal(value)
    if isinstance(value, bool):
        return Literal(value, datatype=XSD.boolean)
    if isinstance(value, int):
        return Literal(value)
    if isinstance(value, float):
        return decimal_literal(value)
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
        return decimal_add(bindings[ve["left"]], bindings[ve["right"]])
    if kind == "numeric_add_constant":
        return decimal_add(bindings[ve["var"]], ve["constant"])
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

    # Same identity construction as the HVAC pipeline (Definitions 3 and 5).
    bind_key = _stable_json(canonical_binding(row))
    aid = hashlib.sha256(_stable_json({"rid": rid, "bindKey": bind_key, "window_id": window_id}).encode("utf-8")).hexdigest()
    bind_key_payload = {k: bindings[k] for k in sorted(bindings) if k != "e"}

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
        "identity": {"rid": rid, "bindKey": bind_key, "window_id": window_id},
        "binding_profile": BINDING_PROFILE,
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
            # Event-role compatibility is part of each shipped SELECT query.
            action = create_action(rule, row, role_rank, window_id, event_role)
            identity = (action["rid"], action["bindKey"], action["window_id"])
            if identity in seen:
                continue
            seen.add(identity)
            actions.append(action)
    return actions


def make_literal(predicate: str, value: Any) -> Literal:
    if predicate in DECIMAL_PREDICATES:
        return decimal_literal(value)
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


_SHAPES_BY_CONTENT: Dict[str, Graph] = {}


def _shapes_graph() -> Graph:
    """Shape graph cached by content identity of the file currently selected
    by SHAPES_PATH (never a stale singleton)."""
    content = Path(SHAPES_PATH).read_bytes()
    key = hashlib.sha256(content).hexdigest()
    if key not in _SHAPES_BY_CONTENT:
        g = Graph()
        g.parse(data=content.decode("utf-8"), format="turtle")
        _SHAPES_BY_CONTENT[key] = g
    return _SHAPES_BY_CONTENT[key]


def check_admissibility_shacl(graph: Graph) -> Tuple[bool, str]:
    from pyshacl import validate
    # The aggregate uses decimal addition, with precision derived from the
    # graph rather than the process-wide decimal context.
    try:
        with localcontext(validation_decimal_context(graph)):
            conforms, _g, text = validate(
                data_graph=graph, shacl_graph=_shapes_graph(),
                inference=None, advanced=True, debug=False,
            )
    except InvalidOperation:
        return False, "Reference validation error: decimal comparison failed; candidate refused"
    return bool(conforms), text


def check_policy_guard(graph: Graph, action: Mapping[str, Any]) -> Tuple[bool, str]:
    if action.get("predicate") != CHARGING_POWER:
        return True, "policy_guard_not_applicable"
    role = str(action.get("role"))
    max_pred = ROLE_MAX_PREDICATES.get(role)
    if max_pred is None:
        return True, "policy_guard_not_applicable"
    proposed = decimal_value(action["value"])
    minimum = decimal_value(policy_literal(graph, POLICY_NODE, MIN_POWER))
    maximum = decimal_value(policy_literal(graph, POLICY_NODE, max_pred))
    if proposed < minimum:
        return False, f"policy_min_violation:{proposed} < {minimum}"
    if proposed > maximum:
        return False, f"policy_role_cap_violation:{role}:{proposed} > {maximum}"
    return True, "policy_guard_passed"


# ---------------------------------------------------------------------------
# Resolution (Definition 9, gates in the normative order (i)-(iv))
# ---------------------------------------------------------------------------

def validate_ev_configuration(graph: Graph) -> None:
    """Mandatory policy and fixed-feeder parameters, even on an empty step."""
    validate_policy_profile(graph, POLICY_NODE, (MIN_POWER, *ROLE_MAX_PREDICATES.values()))
    budget = policy_literal(graph, URIRef(EV + "Feeder1"), URIRef(EV + "feederBudget"))
    if decimal_value(budget) < 0:
        raise PolicyProfileError("feederBudget: expected a nonnegative decimal")


def resolve_actions(
    graph: Graph,
    schedule: List[Mapping[str, Any]],
    conflict_policy: str = CONFLICT_POLICY,
    active_roles: Optional[List[str]] = None,
) -> Tuple[List[Dict[str, Any]], Graph, List[Dict[str, Any]]]:
    validate_conflict_policy(conflict_policy)
    validate_ev_configuration(graph)
    active = set(ACTIVE_ROLES if active_roles is None else active_roles)
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

        # (i) role filter
        if str(action.get("role")) not in active:
            decision.update({"accepted": False, "reason": "inactive_role", "post_graph_digest": pre_digest})
            decisions.append(decision)
            continue

        # (ii) policy guard
        ok, preason = check_policy_guard(current, action)
        if not ok:
            decision.update({"accepted": False, "reason": preason, "policy_reason": preason,
                             "post_graph_digest": pre_digest})
            decisions.append(decision)
            continue

        # (iii) conflict gate
        if conflict_policy == "first_writer_wins" and target_key in accepted_targets:
            w = accepted_targets[target_key]
            decision.update({"accepted": False, "reason": "shadowed_by_prior_accepted_action",
                             "blocked_by_aid": w["aid"], "blocked_by_rid": w["rid"],
                             "post_graph_digest": pre_digest})
            decisions.append(decision)
            continue

        # (iv) admissibility
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
# Trace recording, replay and cross-process re-execution (Section V-K)
# ---------------------------------------------------------------------------

def _canonical_lines(graph: Graph) -> List[str]:
    return canonical_triple_lines(graph)


def _graph_from_lines(lines: List[str]) -> Graph:
    g = Graph()
    if lines:
        g.parse(data="\n".join(lines), format="nt")
    return g


def record_trace(name: str, state: Graph, enabled: List[Dict[str, Any]], role_rank: Mapping[str, int],
                 schedule: List[Mapping[str, Any]], decisions: List[Dict[str, Any]],
                 accepted: List[Dict[str, Any]], successor: Graph,
                 active_roles: Optional[List[str]] = None) -> str:
    path = Path(paths.ev_trace(f"trace_ev_{name}.json"))
    trace = {
        "workload": f"EV charging ({name})",
        "settings": {"schedule_key": SCHEDULE_KEY, "role_rank": dict(role_rank),
                     "active_roles": ACTIVE_ROLES if active_roles is None else active_roles,
                     "conflict_policy": CONFLICT_POLICY,
                     "admissibility_regime": "shacl", "shapes": "shapes/invariants_ev.ttl",
                     "dependencies": {"shapes_path": "shapes/invariants_ev.ttl",
                                      "shapes_sha256": _file_sha256(HERE / "shapes" / "invariants_ev.ttl"),
                                      "rules_path": "data/rules_ev.json",
                                      "rules_sha256": _file_sha256(HERE / "data" / "rules_ev.json"),
                                      "binding_profile": BINDING_PROFILE, "numeric_profile": NUMERIC_PROFILE}},
        "input_graph": {"triples": _canonical_lines(state), "digest": graph_digest(state)},
        "actions": enabled,
        "schedule_aids": [a["aid"] for a in schedule],
        "decisions": _comparable(decisions),
        "accepted_aids": [a["aid"] for a in accepted],
        "successor_digest": graph_digest(successor),
    }
    path.write_text(json.dumps(trace, indent=2, default=str), encoding="utf-8")
    return str(path)


def _comparable(decisions: List[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """All recorded decision fields except the free-text validator report."""
    return [json.loads(json.dumps({k: v for k, v in d.items() if k != "validation_report"},
                                  sort_keys=True, default=str)) for d in decisions]


def _file_sha256(path) -> str:
    return hashlib.sha256(Path(path).read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def replay_trace(path: str) -> Dict[str, Any]:
    trace = json.loads(Path(path).read_text(encoding="utf-8"))
    cfg = trace["settings"]
    deps = cfg.get("dependencies", {})
    state = _graph_from_lines(trace["input_graph"]["triples"])
    dependency_ok = (graph_digest(state) == trace["input_graph"]["digest"]
                     and cfg.get("admissibility_regime") == "shacl"
                     and deps.get("binding_profile") == BINDING_PROFILE
                     and deps.get("numeric_profile") == NUMERIC_PROFILE
                     and _file_sha256(HERE / deps.get("shapes_path", "shapes/invariants_ev.ttl")) == deps.get("shapes_sha256")
                     and _file_sha256(HERE / deps.get("rules_path", "data/rules_ev.json")) == deps.get("rules_sha256"))
    # The recorded configuration drives the reconstruction: role ranks are
    # re-derived from the recorded role-rank map, the recorded shape graph is
    # validated against, and the recorded active-role set is applied.
    actions = deepcopy(trace["actions"])
    for a in actions:
        a["roleRank"] = int(cfg["role_rank"].get(str(a.get("role")), 999))
    schedule = schedule_actions(actions, settings={"schedule_key": cfg["schedule_key"]})
    global SHAPES_PATH
    previous_shapes = SHAPES_PATH
    SHAPES_PATH = str(HERE / deps.get("shapes_path", cfg.get("shapes", "shapes/invariants_ev.ttl")))
    try:
        accepted, successor, decisions = resolve_actions(
            state, schedule, conflict_policy=cfg["conflict_policy"], active_roles=cfg["active_roles"])
    finally:
        SHAPES_PATH = previous_shapes
    rec = _comparable(trace["decisions"])
    regen = _comparable(decisions)
    return {
        "workload": trace["workload"],
        "trace_path": path,
        "dependencies_match": dependency_ok,
        "enabled_recorded": len(trace["actions"]),
        "enabled_replayed": len(schedule),
        "schedule_match": [a["aid"] for a in schedule] == trace["schedule_aids"],
        "decisions_match": rec == regen,
        "decisions_compared": len(rec),
        "accepted_match": [a["aid"] for a in accepted] == trace["accepted_aids"],
        "digest_match": graph_digest(successor) == trace["successor_digest"],
        "successor_digest": trace["successor_digest"],
    }


def cross_process_digest(name: str, hash_seed: str = "12345") -> str:
    """Run this window in a fresh interpreter with a different PYTHONHASHSEED
    and return its successor digest."""
    import subprocess
    env = dict(os.environ, PYTHONHASHSEED=hash_seed, PYTHONDONTWRITEBYTECODE="1")
    proc = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--digest", name],
                          cwd=str(HERE), env=env, capture_output=True, text=True, check=True)
    return proc.stdout.strip().splitlines()[-1]


def run_replay_all() -> List[Dict[str, Any]]:
    """Record, replay and cross-process re-execute every EV window."""
    windows = {
        "headline": lambda: run_headline(),
        "governance_grid_over_fleet": lambda: run_governance()["grid_over_fleet"],
        "governance_fleet_over_grid": lambda: run_governance()["fleet_over_grid"],
        "emergency": lambda: run_emergency(),
    }
    rows = []
    for name, fn in windows.items():
        res = fn()
        row = replay_trace(res["trace_path"])
        row["cross_process_digest_match"] = cross_process_digest(name) == res["successor_digest"]
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# Workloads
# ---------------------------------------------------------------------------

def _pipeline(events: List[Dict[str, Any]], role_rank: Mapping[str, int],
              state: Optional[Graph] = None):
    if state is None:
        state = load_state(BASE_GRAPH_PATH)
    validate_ev_configuration(state)
    rules = load_rules(RULES_PATH)
    dataset, meta = build_dataset(state, events)
    enabled = evaluate_rules(dataset, rules, role_rank, meta["window_id"])
    schedule = schedule_actions(enabled, settings={"schedule_key": SCHEDULE_KEY})
    return state, enabled, schedule


def _primed_state(charging: Mapping[str, float], connectors: Optional[Mapping[str, str]] = None) -> Graph:
    """Base EV graph with some charging points already drawing power."""
    g = load_state(BASE_GRAPH_PATH)
    for cp, val in charging.items():
        s = URIRef(f"{EV}{cp}")
        p = URIRef(CHARGING_POWER)
        for t in list(g.triples((s, p, None))):
            g.remove(t)
        g.add((s, p, decimal_literal(val)))
        if decimal_value(val) > 0:
            g.set((s, URIRef(f"{EV}connectorState"), Literal("charging")))
    for cp, label in (connectors or {}).items():
        g.set((URIRef(f"{EV}{cp}"), URIRef(f"{EV}connectorState"), Literal(label)))
    return g


def run_emergency() -> Dict[str, Any]:
    """
    Grid-emergency window. State is primed with CP1=CP2=22 kW. A GridEmergency
    fires q5 (reduce points above 5 kW to 5 kW, priority 0) and q4 (assert curtailment,
    priority 1). The lower priority number on q5 means load reductions are
    committed BEFORE curtailment is asserted, so the cross-target
    curtailment-consistency shape is satisfied along the committed path.
    """
    state = _primed_state({"CP1": 22, "CP2": 22})
    events = [{"eid": "em-001", "timestamp": "2026-03-14T09:00:00Z", "type": "GridEmergency",
               "role": "grid", "payload": {"feeder": f"{EV}Feeder1"}}]
    _state, enabled, schedule = _pipeline(events, ROLE_RANK, state=state)
    accepted, successor, decisions = resolve_actions(state, schedule)
    trace_path = record_trace("emergency", state, enabled, ROLE_RANK, schedule, decisions, accepted, successor)

    powers, feeder_state = {}, None
    for s, p, o in successor:
        if str(p) == CHARGING_POWER:
            powers[str(s).split("#")[-1]] = float(o.toPython())
        if str(p) == f"{EV}feederState":
            feeder_state = str(o.toPython())
    powers = dict(sorted(powers.items()))
    ok, _ = check_admissibility_shacl(successor)
    return {
        "trace_path": trace_path,
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
    trace_path = record_trace("headline", state, enabled, ROLE_RANK, schedule, decisions, accepted, successor)

    committed_powers = {}
    for s, p, o in successor:
        if str(p) == CHARGING_POWER:
            committed_powers[str(s).split("#")[-1]] = float(o.toPython())
    committed_powers = dict(sorted(committed_powers.items()))

    final_ok, _ = check_admissibility_shacl(successor)
    return {
        "trace_path": trace_path,
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
    for label, rank in (("grid_over_fleet", ROLE_RANK),
                        ("fleet_over_grid", {"safety": 0, "fleet": 1, "grid": 2, "driver": 3})):
        state, enabled, schedule = _pipeline(events, rank)
        accepted, successor, decisions = resolve_actions(state, schedule)
        trace_path = record_trace(f"governance_{label}", state, enabled, rank, schedule, decisions, accepted, successor)
        committed = None
        for s, p, o in successor:
            if str(p) == CHARGING_POWER and str(s).endswith("CP1"):
                committed = float(o.toPython())
        ok, _ = check_admissibility_shacl(successor)
        out[label] = {
            "trace_path": trace_path,
            "successor_digest": graph_digest(successor),
            "schedule_rids": [a["rid"] for a in schedule],
            "accepted_rids": [a["rid"] for a in accepted],
            "committed_power_CP1": committed,
            "final_admissible": ok,
            "decisions": [{"rid": d["rid"], "value": d["target"]["value"],
                           "accepted": d["accepted"], "reason": d["reason"]} for d in decisions],
        }
    return out


def _digest_only(name: str) -> str:
    if name == "headline":
        return run_headline()["successor_digest"]
    if name == "emergency":
        return run_emergency()["successor_digest"]
    if name.startswith("governance_"):
        return run_governance()[name[len("governance_"):]]["successor_digest"]
    raise ValueError(name)


def verification_failures(summary: Mapping[str, Any]) -> List[str]:
    """Fail closed on missing/failed evidence from each reported EV workload."""
    failures = []
    expected_windows = {f"EV charging ({name})" for name in
                        ("headline", "emergency", "governance_grid_over_fleet",
                         "governance_fleet_over_grid")}
    rows = summary.get("replay", [])
    if (len(rows) != len(expected_windows)
            or {r.get("workload") for r in rows} != expected_windows):
        failures.append("replay must contain each of the four expected windows exactly once")
    replay_checks = ("dependencies_match", "schedule_match", "decisions_match",
                     "accepted_match", "digest_match", "cross_process_digest_match")
    for row in rows:
        for check in replay_checks:
            if row.get(check) is not True:
                failures.append(f"{row.get('workload', 'unknown')}: {check}")
    for name in ("headline", "emergency"):
        if summary.get(name, {}).get("final_admissible") is not True:
            failures.append(f"{name}: final_admissible")
    for name in ("grid_over_fleet", "fleet_over_grid"):
        if summary.get("governance", {}).get(name, {}).get("final_admissible") is not True:
            failures.append(f"governance_{name}: final_admissible")
    stress = summary.get("headline_30_trials", {})
    if (stress.get("runs") != 30 or stress.get("ours_unique_states") != 1
            or stress.get("ours_admissible_pct") != 100.0):
        failures.append("headline trials: expected 30 admissible runs and one successor state")
    return failures


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--digest":
        print(_digest_only(sys.argv[2]))
        return

    out_dir = paths.EV
    paths.ensure_dirs()

    headline = run_headline()
    stress = run_headline_30_vs_random()
    governance = run_governance()
    emergency = run_emergency()
    replay = run_replay_all()

    summary = {"headline": headline, "headline_30_trials": stress,
               "governance": governance, "emergency": emergency, "replay": replay}
    failures = verification_failures(summary)
    summary["verification"] = {"overall_pass": not failures, "failures": failures}
    with open(out_dir / "experiment_ev.json", "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2, default=str)

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

    print("\n== EV replay (trace-based) and cross-process re-execution ==")
    for r in replay:
        print(f"  {r['workload']:<40} schedule={'match' if r['schedule_match'] else 'MISMATCH'} "
              f"decisions={r['decisions_compared']}/{r['decisions_compared'] if r['decisions_match'] else 'MISMATCH'} "
              f"digest={'match' if r['digest_match'] else 'MISMATCH'} "
              f"cross-process={'match' if r['cross_process_digest_match'] else 'MISMATCH'}")

    print("\nWrote", out_dir / "experiment_ev.json")
    if failures:
        raise SystemExit("EV verification failed: " + "; ".join(failures))


if __name__ == "__main__":
    main()
