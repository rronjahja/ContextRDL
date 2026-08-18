"""
Drop-in replacement for the original ``admissibility.py``.

Exposes three entry points:
  * ``check_admissibility_shacl``       -- authoritative, uses pySHACL against invariants.ttl.
  * ``check_admissibility_incremental`` -- fast, zone-scoped equivalent of the shapes.
  * ``check_admissibility``             -- dispatcher with the *original* signature,
                                           so ``resolver.py`` keeps working unchanged.

The dispatcher picks between the two via env var ``ADMISSIBILITY_REGIME``:
  * ``incremental`` (default): fast path for in-loop use.
  * ``shacl``:                 real pySHACL. Slower; used by experiments that
                               want to pay the cost to get the specification
                               validator.

The equivalence of the two validators is asserted by
``test_validator_equivalence.py`` -- run that first.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Iterable, List, Optional, Set, Tuple

from rdflib import Graph, Namespace, URIRef
from rdflib.namespace import RDF

EX = Namespace("http://example.org/building#")
ALLOWED_VENTILATION = {"off", "normal", "high", "emergency"}

_DEFAULT_SHAPES_PATH = os.environ.get("SHAPES_PATH", "shapes/invariants.ttl")


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


# ---------------------------------------------------------------------------
# Authoritative: pySHACL against invariants.ttl
# ---------------------------------------------------------------------------

@lru_cache(maxsize=8)
def _load_shapes_graph(shapes_path: str) -> Graph:
    """
    Load and cache the shapes graph. We read the file ourselves and hand the
    bytes to rdflib via ``data=`` so Windows absolute paths (e.g. ``G:\\...``)
    are never interpreted as URL schemes by rdflib's auto-parser.
    """
    g = Graph()
    p = Path(shapes_path)
    if not p.is_absolute() and not p.exists():
        p = Path(__file__).resolve().parent.parent / p
    text = p.read_text(encoding="utf-8")
    g.parse(data=text, format="turtle")
    return g


def check_admissibility_shacl(
    graph: Graph,
    shapes_path: Optional[str] = None,
) -> Tuple[bool, str]:
    """Validate ``graph`` against the SHACL shapes in ``shapes_path``."""
    try:
        from pyshacl import validate
    except ImportError as e:  # pragma: no cover
        raise RuntimeError(
            "pyshacl is not installed. Run: pip install pyshacl"
        ) from e

    path = shapes_path or _DEFAULT_SHAPES_PATH
    shapes_graph = _load_shapes_graph(path)

    conforms, _results_graph, results_text = validate(
        data_graph=graph,
        shacl_graph=shapes_graph,
        inference=None,
        advanced=True,        # enables SPARQL-based constraints
        debug=False,
    )
    return bool(conforms), results_text


# ---------------------------------------------------------------------------
# Fast path: zone-scoped hand-coded equivalent of the SHACL shapes
# ---------------------------------------------------------------------------

def check_admissibility_incremental(
    graph: Graph,
    focus_zones: Optional[Iterable[URIRef]] = None,
) -> Tuple[bool, str]:
    """
    Fast equivalent of the SHACL shapes in invariants.ttl.

    If ``focus_zones`` is given, only those zones are checked -- this is safe
    when we know which zones an action touched (HVAC zone constraints only
    depend on the zone itself). Otherwise every HVAC_Zone is checked.
    """
    violations: List[str] = []

    if focus_zones is None:
        zones_iter = sorted(graph.subjects(RDF.type, EX.HVAC_Zone), key=str)
    else:
        zones_iter = sorted(focus_zones, key=str)

    for zone in zones_iter:
        # --- ZoneSetpointShape: exactly 1 value in [18.0, 26.0] ---
        setpoints = list(graph.objects(zone, EX.currentSetpoint))
        sp_value: Optional[float] = None
        if len(setpoints) != 1:
            violations.append(
                f"ZoneSetpointShape: {_fmt(zone)} currentSetpoint cardinality != 1 (found {len(setpoints)})"
            )
        else:
            try:
                sp_value = float(setpoints[0].toPython())
            except Exception:
                violations.append(f"ZoneSetpointShape: {_fmt(zone)} setpoint not numeric")
                sp_value = None
            else:
                if sp_value < 18.0:
                    violations.append(f"ZoneSetpointShape: {_fmt(zone)} setpoint {sp_value} < 18.0")
                if sp_value > 26.0:
                    violations.append(f"ZoneSetpointShape: {_fmt(zone)} setpoint {sp_value} > 26.0")

        # --- ZoneVentilationShape: exactly 1 mode in the allowed set ---
        modes = list(graph.objects(zone, EX.ventilationMode))
        mode_value: Optional[str] = None
        if len(modes) != 1:
            violations.append(
                f"ZoneVentilationShape: {_fmt(zone)} ventilationMode cardinality != 1"
            )
        else:
            mode_value = str(modes[0].toPython())
            if mode_value not in ALLOWED_VENTILATION:
                violations.append(
                    f'ZoneVentilationShape: {_fmt(zone)} ventilationMode "{mode_value}" not in allowed set'
                )

        # --- EmergencyStateShape: exactly 1 boolean value ---
        estates = list(graph.objects(zone, EX.emergencyState))
        estate_value: Optional[bool] = None
        if len(estates) != 1:
            violations.append(
                f"EmergencyStateShape: {_fmt(zone)} emergencyState cardinality != 1"
            )
        else:
            raw = estates[0].toPython()
            if not isinstance(raw, bool):
                violations.append(
                    f"EmergencyStateShape: {_fmt(zone)} emergencyState not boolean"
                )
            else:
                estate_value = raw

        # --- ZoneAComfortCapShape: ZoneA setpoint <= 23 ---
        if zone == EX.ZoneA and sp_value is not None and sp_value > 23.0:
            violations.append(
                f"ZoneAComfortCapShape: {_fmt(zone)} setpoint {sp_value} > 23.0 ZoneA cap"
            )

        # --- ZoneAEmergencyVentShape: if ZoneA vent=="off" then setpoint <= 21
        # (SHACL uses FILTER (?sp > 21.0) -- strict inequality) ---
        if (
            zone == EX.ZoneA
            and mode_value == "off"
            and sp_value is not None
            and sp_value > 21.0
        ):
            violations.append(
                f'ZoneAEmergencyVentShape: {_fmt(zone)} vent="off" requires setpoint <= 21.0, found {sp_value}'
            )

        # --- EmergencyVentLockShape: emergencyState true => vent="emergency" ---
        if estate_value is True and mode_value is not None and mode_value != "emergency":
            violations.append(
                f'EmergencyVentLockShape: {_fmt(zone)} emergencyState=true but vent="{mode_value}"'
            )

    return len(violations) == 0, _report(violations)


# ---------------------------------------------------------------------------
# Original signature dispatcher -- resolver.py imports this
# ---------------------------------------------------------------------------

def check_admissibility(
    graph: Graph,
    shapes_path: Optional[str] = None,
) -> Tuple[bool, str]:
    """
    Original entry point. Preserves the signature ``(graph, shapes_path)``
    so the existing ``resolver.py`` works unchanged.

    Regime selected by env var ``ADMISSIBILITY_REGIME``:
      * ``incremental`` (default)  -- fast zone-scoped checks
      * ``shacl``                 -- real pySHACL validation
    """
    regime = os.environ.get("ADMISSIBILITY_REGIME", "incremental").lower()
    if regime == "shacl":
        return check_admissibility_shacl(graph, shapes_path)
    return check_admissibility_incremental(graph)


if __name__ == "__main__":
    g = Graph()
    g.parse("shapes/base_graph.ttl", format="turtle")

    print("--- incremental ---")
    ok, rep = check_admissibility_incremental(g)
    print("admissible:", ok)
    print(rep)

    print("--- pySHACL ---")
    try:
        ok, rep = check_admissibility_shacl(g, "shapes/invariants.ttl")
        print("admissible:", ok)
        print(rep)
    except RuntimeError as e:
        print("skipped:", e)