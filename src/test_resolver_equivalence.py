"""
Regression test: the incremental resolver must produce identical accepted
sets, decision keys (aid, accepted, reason), and successor digest as the
original resolver across several workloads.

If this test FAILS, do not trust the scalability numbers -- the incremental
resolver has diverged semantically from the original.

Run:
    python test_resolver_equivalence.py
Exit 0 on full equivalence, 1 on any mismatch.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from copy import deepcopy
from typing import Any, Dict, List, Mapping

from rdflib import Graph

from dataset_builder import build_dataset, load_events, load_state
from experiment_helpers import (
    governance_conflict_events,
    tie_conflict_events,
    duplicate_identity_events,
)
from resolver import resolve_actions as resolve_original
from resolver_incremental import resolve_actions_incremental
from rule_engine import (
    evaluate_rules,
    load_settings,
    resolve_governance_context,
    schedule_actions,
)
from rule_loader import load_rules


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _graph_digest(graph: Graph) -> str:
    lines = sorted(f"{s.n3()} {p.n3()} {o.n3()} ." for s, p, o in graph)
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def _key_triplets(decisions: List[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    return [
        {
            "aid": d.get("aid"),
            "accepted": d.get("accepted"),
            "reason": d.get("reason"),
            "target_key": d.get("target_key"),
        }
        for d in decisions
    ]


def _run_workload(label: str,
                  events: List[Dict[str, Any]],
                  settings_override: Dict[str, Any] = None) -> Dict[str, Any]:
    settings = load_settings("configs/settings.json")
    if settings_override:
        def _deep_update(b, u):
            for k, v in u.items():
                if isinstance(v, dict) and isinstance(b.get(k), dict):
                    b[k] = _deep_update(dict(b[k]), v)
                else:
                    b[k] = v
            return b
        settings = _deep_update(settings, deepcopy(settings_override))

    context = resolve_governance_context(settings=settings, contexts_path="data/contexts.json")
    state = load_state("shapes/base_graph.ttl")
    rules = load_rules(settings.get("paths", {}).get("rules", "configs/rules.json"))
    dataset, meta = build_dataset(state, events, settings=settings)

    enabled = evaluate_rules(dataset, rules, settings=settings, context=context, window_meta=meta)
    schedule = schedule_actions(enabled, settings=settings)

    # Original resolver path
    accepted_o, successor_o, decisions_o = resolve_original(
        state, schedule, shapes_path="shapes/invariants.ttl", settings=settings
    )

    # Incremental resolver path  (fresh clone of state)
    state2 = Graph()
    for t in state:
        state2.add(t)
    accepted_n, successor_n, decisions_n = resolve_actions_incremental(
        state2, schedule, shapes_path="shapes/invariants.ttl",
        settings=settings, record_digests=True,
    )

    return {
        "label": label,
        "schedule_aids": [a["aid"] for a in schedule],
        "accepted_aids_orig": [a["aid"] for a in accepted_o],
        "accepted_aids_new":  [a["aid"] for a in accepted_n],
        "decisions_orig": _key_triplets(decisions_o),
        "decisions_new":  _key_triplets(decisions_n),
        "successor_digest_orig": _graph_digest(successor_o),
        "successor_digest_new":  _graph_digest(successor_n),
    }


def _compare(run: Dict[str, Any]) -> Dict[str, Any]:
    ok_accepted  = run["accepted_aids_orig"]    == run["accepted_aids_new"]
    ok_decisions = run["decisions_orig"]        == run["decisions_new"]
    ok_digest    = run["successor_digest_orig"] == run["successor_digest_new"]
    return {
        "label":           run["label"],
        "accepted_match":  ok_accepted,
        "decisions_match": ok_decisions,
        "digest_match":    ok_digest,
        "all_match":       ok_accepted and ok_decisions and ok_digest,
    }


# ---------------------------------------------------------------------------
# Workloads
# ---------------------------------------------------------------------------

def workloads():
    default_events = load_events("data/events.jsonl")
    yield "default",               default_events, None
    yield "governance_reversed",   default_events, {"role_precedence": {"operator": 2, "occupant": 1}}
    yield "tie_conflict",          tie_conflict_events(), None
    yield "governance_conflict",   governance_conflict_events(), None
    yield ("governance_conflict_reversed",
           governance_conflict_events(),
           {"role_precedence": {"operator": 2, "occupant": 1}})
    yield "duplicate_identity",    duplicate_identity_events(), None


def main():
    results = []
    for label, events, override in workloads():
        run = _run_workload(label, events, override)
        check = _compare(run)
        results.append({"run": run, "check": check})

    for rec in results:
        c = rec["check"]
        status = "PASS" if c["all_match"] else "FAIL"
        print(f"[{status}] {c['label']}: accepted={c['accepted_match']} "
              f"decisions={c['decisions_match']} digest={c['digest_match']}")

    overall = all(r["check"]["all_match"] for r in results)

    summary = {
        "overall_pass": overall,
        "per_workload": [r["check"] for r in results],
        "failures": [
            {
                "label": r["run"]["label"],
                "successor_orig": r["run"]["successor_digest_orig"],
                "successor_new":  r["run"]["successor_digest_new"],
                "decisions_orig": r["run"]["decisions_orig"],
                "decisions_new":  r["run"]["decisions_new"],
            }
            for r in results if not r["check"]["all_match"]
        ],
    }
    os.makedirs("results", exist_ok=True)
    with open("results/test_resolver_equivalence.json", "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2)

    if overall:
        print(f"\nALL PASSED: {len(results)} workloads match original resolver.")
    else:
        print(f"\nFAILED: see results/test_resolver_equivalence.json for diffs.")
    sys.exit(0 if overall else 1)


if __name__ == "__main__":
    main()
