from __future__ import annotations

from typing import List

from rdflib import Graph, Namespace
from rdflib.namespace import RDF

EX = Namespace("http://example.org/building#")
ALLOWED_VENTILATION = {"off", "normal", "high", "emergency"}


def _fmt(node) -> str:
    try:
        return node.n3()
    except Exception:
        return str(node)


def _report(violations: List[str]) -> str:
    if not violations:
        return "Validation Report\nConforms: True\n"
    lines = ["Validation Report", "Conforms: False", f"Results ({len(violations)}):"]
    lines.extend(violations)
    return "\n".join(lines) + "\n"


def check_admissibility(graph: Graph, shapes_path: str | None = None):
    violations: List[str] = []

    zones = set(graph.subjects(RDF.type, EX.HVAC_Zone))
    for zone in sorted(zones, key=str):
        setpoints = list(graph.objects(zone, EX.currentSetpoint))
        if len(setpoints) != 1:
            violations.append(
                f"Constraint Violation: {_fmt(zone)} ex:currentSetpoint must have exactly 1 value; found {len(setpoints)}."
            )
        else:
            try:
                value = float(setpoints[0].toPython())
            except Exception:
                violations.append(
                    f"Constraint Violation: {_fmt(zone)} ex:currentSetpoint is not numeric ({_fmt(setpoints[0])})."
                )
            else:
                if value < 18.0:
                    violations.append(
                        f"Constraint Violation: {_fmt(zone)} ex:currentSetpoint {value} is below 18.0."
                    )
                if value > 26.0:
                    violations.append(
                        f"Constraint Violation: {_fmt(zone)} ex:currentSetpoint {value} is above 26.0."
                    )

        modes = list(graph.objects(zone, EX.ventilationMode))
        if len(modes) != 1:
            violations.append(
                f"Constraint Violation: {_fmt(zone)} ex:ventilationMode must have exactly 1 value; found {len(modes)}."
            )
        else:
            mode = str(modes[0].toPython())
            if mode not in ALLOWED_VENTILATION:
                violations.append(
                    f'Constraint Violation: {_fmt(zone)} ex:ventilationMode "{mode}" is not an allowed value.'
                )

        emergency_states = list(graph.objects(zone, EX.emergencyState))
        if len(emergency_states) != 1:
            violations.append(
                f"Constraint Violation: {_fmt(zone)} ex:emergencyState must have exactly 1 value; found {len(emergency_states)}."
            )
        else:
            state_value = emergency_states[0].toPython()
            if not isinstance(state_value, bool):
                violations.append(
                    f"Constraint Violation: {_fmt(zone)} ex:emergencyState is not boolean ({_fmt(emergency_states[0])})."
                )

    zone_a = EX.ZoneA
    zone_a_setpoints = list(graph.objects(zone_a, EX.currentSetpoint))
    if len(zone_a_setpoints) == 1:
        try:
            zone_a_value = float(zone_a_setpoints[0].toPython())
            if zone_a_value > 23.0:
                violations.append(
                    f"Constraint Violation: {_fmt(zone_a)} ex:currentSetpoint {zone_a_value} exceeds the ZoneA comfort cap of 23.0."
                )
        except Exception:
            pass

    zone_a_modes = list(graph.objects(zone_a, EX.ventilationMode))
    if len(zone_a_modes) == 1 and len(zone_a_setpoints) == 1:
        try:
            zone_a_sp = float(zone_a_setpoints[0].toPython())
            zone_a_mode = str(zone_a_modes[0].toPython())
            if zone_a_mode == "off" and zone_a_sp > 21.0:
                violations.append(
                    f'Constraint Violation: {_fmt(zone_a)} ventilationMode "off" requires currentSetpoint <= 21.0, found {zone_a_sp}.'
                )
        except Exception:
            pass

    for zone in sorted(zones, key=str):
        states = list(graph.objects(zone, EX.emergencyState))
        modes = list(graph.objects(zone, EX.ventilationMode))
        if len(states) == 1 and len(modes) == 1:
            try:
                is_emergency = bool(states[0].toPython())
                mode = str(modes[0].toPython())
                if is_emergency and mode != "emergency":
                    violations.append(
                        f'Constraint Violation: {_fmt(zone)} has emergencyState true but ventilationMode "{mode}" instead of "emergency".'
                    )
            except Exception:
                pass

    return len(violations) == 0, _report(violations)


if __name__ == "__main__":
    graph = Graph()
    graph.parse("shapes/base_graph.ttl", format="turtle")
    conforms, report = check_admissibility(graph, "shapes/invariants.ttl")
    print("Admissible:", conforms)
    print(report)