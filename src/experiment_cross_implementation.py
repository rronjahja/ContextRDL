"""
Cross-implementation reproducibility (reviewer concern R1-7).

Runs the clean-room second implementation (independent_resolver.py) on the
canonical inputs recorded in each workload trace and compares, field by
field, against the primary implementation's recorded outputs:

  * schedule (order of action identifiers),
  * per-action accept/reject decisions and reason class,
  * accepted set,
  * successor-graph digest.

Traces are (re)generated freshly by the primary implementation for every
workload family, then handed to the second implementation.

Usage:
    python experiment_cross_implementation.py
Writes results/hvac/experiment_cross_implementation.json
"""
from __future__ import annotations
import paths  # noqa: E402  (results layout)

import json
import random
from pathlib import Path
from typing import Any, Dict, List

from rdflib import Graph

import independent_resolver as second
from engine import run_engine

# --- workloads ----------------------------------------------------------------
# The tie-conflict and governance-conflict workloads are the SAME event lists
# used by experiment_hvac_v3.py (Table 6) and the replay table, imported from
# experiment_helpers so that a workload name means one thing throughout the
# paper. Two further traces exercise the role-rank construction (reversed
# precedence) and gate (i) (an inactive role).

from experiment_helpers import governance_conflict_events, tie_conflict_events  # noqa: E402

WORKLOADS: Dict[str, Dict[str, Any]] = {
    "default": {},
    "tie_conflict": {"events": tie_conflict_events()},
    "governance_conflict": {"events": governance_conflict_events()},
    "governance_conflict_reversed": {
        "events": governance_conflict_events(),
        "settings_override": {"role_precedence": {"occupant": 0, "operator": 1, "emergency": 2}},
    },
    "default_occupant_inactive": {"context_name": "occupant-inactive"},
}


def reason_class(reason: str) -> str:
    """Reason-code class = the text before the first colon (Definition 11 (x));
    a detail suffix after the colon is implementation-specific and not compared."""
    return str(reason).split(":", 1)[0]


def compare(trace: Dict[str, Any], shapes_graph: Graph) -> Dict[str, Any]:
    input_graph = Graph()
    input_graph.parse(data="\n".join(trace["input_graph"]["triples"]), format="nt")

    settings = trace["settings"]
    context = (trace.get("window") or {}).get("governance_context") or {}
    config = {
        "schedule_key": settings.get("schedule_key"),
        "conflict_policy": settings.get("governance", {}).get("conflict_policy", "first_writer_wins"),
        "active_roles": context.get("active_roles"),
        "enforce_active_roles": context.get("enforce_active_roles", False),
    }

    # canonical UNORDERED inputs: the constructed action instances, handed over
    # in a seeded random order so that the second implementation's schedule
    # cannot inherit the primary implementation's ordering. Pipeline traces
    # carry them under "schedule"; constructed-action traces under "actions".
    actions = list(trace["schedule"] if "schedule" in trace else trace["actions"])
    random.Random(20260909).shuffle(actions)

    result = second.resolve(input_graph, actions, config, shapes_graph)

    recorded_schedule = [d["aid"] for d in sorted(trace["decisions"], key=lambda d: d["schedule_index"])]
    recorded_post = [d.get("post_graph_digest") for d in sorted(trace["decisions"], key=lambda d: d["schedule_index"])]
    independent_post = [d.get("post_graph_digest") for d in result["decisions"]]
    recorded_decisions = [
        (d["aid"], bool(d["accepted"]), reason_class(str(d["reason"])))
        for d in sorted(trace["decisions"], key=lambda d: d["schedule_index"])
    ]
    second_decisions = [
        (d["aid"], bool(d["accepted"]), reason_class(str(d["reason"])))
        for d in result["decisions"]
    ]
    recorded_accepted = trace["accepted_aids"] if "accepted_aids" in trace else [a["aid"] for a in trace["accepted_actions"]]
    recorded_digest = trace["successor_digest"] if "successor_digest" in trace else trace["successor_graph"]["digest"]
    recorded_reason_classes = sorted({reason_class(str(d["reason"])) for d in trace["decisions"]})

    return {
        "schedule_match": recorded_schedule == result["schedule_aids"],
        "decisions_match": recorded_decisions == second_decisions,
        "accepted_match": recorded_accepted == result["accepted_aids"],
        "post_state_digests_match": recorded_post == independent_post,
        "digest_match": recorded_digest == result["successor_digest"],
        "reason_classes_exercised": recorded_reason_classes,
        "recorded_digest": recorded_digest,
        "independent_digest": result["successor_digest"],
        "n_actions": len(actions),
    }


def main():
    shapes_graph = Graph()
    shapes_path = Path(__file__).resolve().parent.parent / "shapes" / "invariants.ttl"
    shapes_graph.parse(data=shapes_path.read_text(encoding="utf-8"), format="turtle")

    outcomes: Dict[str, Any] = {}
    for name, kwargs in WORKLOADS.items():
        *_, trace = run_engine(save_trace_file=False, **kwargs)
        outcomes[name] = compare(trace, shapes_graph)

    # Constructed-action traces of the governance-clean workload (Table 9):
    # the only HVAC cases with an admissibility-only rejection.
    from experiment_replay_extra import record_governance_clean  # noqa: E402
    for name, label, ranks in (
        ("governance_clean_op_gt_occ", "op > occ", {"emergency": 0, "operator": 1, "occupant": 2}),
        ("governance_clean_occ_gt_op", "occ > op", {"emergency": 0, "occupant": 1, "operator": 2}),
    ):
        trace = record_governance_clean(label, ranks, paths.hvac_trace(f"trace_{name}.json"))
        outcomes[name] = compare(trace, shapes_graph)

    for name, o in outcomes.items():
        print(f"{name:<28} n={o['n_actions']}  schedule={o['schedule_match']}  decisions={o['decisions_match']}  "
              f"post-state={o['post_state_digests_match']}  accepted={o['accepted_match']}  digest={o['digest_match']}  "
              f"reasons={o['reason_classes_exercised']}")

    out = Path(paths.hvac("experiment_cross_implementation.json"))
    out.write_text(json.dumps(outcomes, indent=2), encoding="utf-8")
    print("Wrote", out)
    ok = all(o["schedule_match"] and o["decisions_match"] and o["accepted_match"]
             and o["post_state_digests_match"] and o["digest_match"] for o in outcomes.values())
    if not ok:
        raise SystemExit("cross-implementation disagreement")
    return outcomes


if __name__ == "__main__":
    main()
