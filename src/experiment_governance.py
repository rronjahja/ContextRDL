from __future__ import annotations

from numpy import trace
from rdflib import URIRef

from engine import run_engine
from experiment_helpers import governance_conflict_events
from trace import save_trace

CURRENT_SETPOINT = URIRef("http://example.org/building#currentSetpoint")
ZONE_B = URIRef("http://example.org/building#ZoneB")


def _zone_value(graph, zone: URIRef, predicate: URIRef):
    for obj in graph.objects(zone, predicate):
        if hasattr(obj, "toPython"):
            return obj.toPython()
        return str(obj)
    return None


def run_case(name: str, operator_rank: int, occupant_rank: int):
    settings_override = {
        "role_precedence": {
            "operator": operator_rank,
            "occupant": occupant_rank,
        }
    }

    _, schedule, accepted, graph_next, trace = run_engine(
        events=governance_conflict_events(),
        settings_override=settings_override,
        save_trace_file=False,
    )

    trace_file = "results/trace_governance_conflict_%d_%d.json" % (operator_rank, occupant_rank)
    save_trace(trace, trace_file)
    print("Saved trace:", trace_file)
    final_zone_b = _zone_value(graph_next, ZONE_B, CURRENT_SETPOINT)

    print(f"\n{name}")
    print("Role precedence:")
    print("operator:", operator_rank, "occupant:", occupant_rank)

    print("Schedule:")
    for action in schedule:
        print(action["rid"], action["aid"], action["target_key"], action["value"])

    print("Accepted actions:")
    for action in accepted:
        print(action["rid"], action["aid"], action["target_key"], action["value"])

    print("Final ZoneB currentSetpoint:", final_zone_b)
    print("Successor graph digest:", trace["successor_graph"]["digest"])
    print("Resolver decisions:")
    for decision in trace["decisions"]:
        print(decision["rid"], decision["accepted"], decision["reason"])

    return {
        "case": name,
        "operator_rank": operator_rank,
        "occupant_rank": occupant_rank,
        "final_zone_b_currentSetpoint": final_zone_b,
        "successor_digest": trace["successor_graph"]["digest"],
        "accepted_actions": [action["aid"] for action in accepted],
    }


if __name__ == "__main__":
    run_case("Case 1: operator higher", 1, 2)
    run_case("Case 2: occupant higher", 2, 1)
