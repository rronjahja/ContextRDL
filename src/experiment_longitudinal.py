from __future__ import annotations

from rdflib import URIRef

from engine import run_engine
from experiment_helpers import longitudinal_event_stream


ZONE_A = URIRef("http://example.org/building#ZoneA")
ZONE_B = URIRef("http://example.org/building#ZoneB")
CURRENT_SETPOINT = URIRef("http://example.org/building#currentSetpoint")
VENTILATION_MODE = URIRef("http://example.org/building#ventilationMode")


def _zone_value(graph, zone: URIRef, predicate: URIRef):
    for obj in graph.objects(zone, predicate):
        if hasattr(obj, "toPython"):
            return obj.toPython()
        return str(obj)
    return None


def run_longitudinal_once():
    stream = longitudinal_event_stream()

    _, _, accepted_step_1, graph_step_1, trace_step_1 = run_engine(
        events=stream,
        anchor_timestamp="2026-03-14T09:00:00Z",
        save_trace_file=False,
    )

    _, _, accepted_step_2, graph_step_2, trace_step_2 = run_engine(
        state_graph=graph_step_1,
        events=stream,
        anchor_timestamp="2026-03-14T09:06:00Z",
        save_trace_file=False,
    )

    _, _, accepted_step_3, graph_step_3, trace_step_3 = run_engine(
        state_graph=graph_step_2,
        events=stream,
        anchor_timestamp="2026-03-14T09:11:00Z",
        save_trace_file=False,
    )

    return {
        "step_1": {
            "accepted_count": len(accepted_step_1),
            "window_id": trace_step_1["window"]["window_id"],
            "successor_digest": trace_step_1["successor_graph"]["digest"],
            "zone_b_currentSetpoint": _zone_value(graph_step_1, ZONE_B, CURRENT_SETPOINT),
            "zone_b_ventilationMode": _zone_value(graph_step_1, ZONE_B, VENTILATION_MODE),
        },
        "step_2": {
            "accepted_count": len(accepted_step_2),
            "window_id": trace_step_2["window"]["window_id"],
            "successor_digest": trace_step_2["successor_graph"]["digest"],
            "zone_b_currentSetpoint": _zone_value(graph_step_2, ZONE_B, CURRENT_SETPOINT),
            "zone_b_ventilationMode": _zone_value(graph_step_2, ZONE_B, VENTILATION_MODE),
        },
        "step_3": {
            "accepted_count": len(accepted_step_3),
            "window_id": trace_step_3["window"]["window_id"],
            "successor_digest": trace_step_3["successor_graph"]["digest"],
            "zone_a_currentSetpoint": _zone_value(graph_step_3, ZONE_A, CURRENT_SETPOINT),
        },
    }


def run_longitudinal_experiment(runs: int = 30):
    sequences = []
    for _ in range(runs):
        summary = run_longitudinal_once()
        sequences.append(
            (
                summary["step_1"]["successor_digest"],
                summary["step_2"]["successor_digest"],
                summary["step_3"]["successor_digest"],
            )
        )

    unique_sequences = sorted(set(sequences))
    example = run_longitudinal_once()

    print("Runs:", runs)
    print("Unique three-step digest sequences:", len(unique_sequences))
    print("Step 1 digest:", example["step_1"]["successor_digest"])
    print("Step 1 ZoneB currentSetpoint:", example["step_1"]["zone_b_currentSetpoint"])
    print("Step 2 digest:", example["step_2"]["successor_digest"])
    print("Step 2 ZoneB currentSetpoint:", example["step_2"]["zone_b_currentSetpoint"])
    print("Step 2 ZoneB ventilationMode:", example["step_2"]["zone_b_ventilationMode"])
    print("Step 3 digest:", example["step_3"]["successor_digest"])
    print("Step 3 ZoneA currentSetpoint:", example["step_3"]["zone_a_currentSetpoint"])

    return {
        "runs": runs,
        "unique_sequences": len(unique_sequences),
        "example": example,
        "sequences": unique_sequences,
    }


if __name__ == "__main__":
    run_longitudinal_experiment()
