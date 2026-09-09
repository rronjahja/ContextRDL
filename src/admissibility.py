"""
Drop-in replacement for the original ``admissibility.py``.

Exposes three entry points:
  * ``check_admissibility_shacl``       -- authoritative, uses pySHACL against invariants.ttl.
  * ``check_admissibility_incremental`` -- guarded fast path for the shipped shapes.
  * ``check_admissibility``             -- dispatcher with the *original* signature,
                                           so ``resolver.py`` keeps working unchanged.

The dispatcher picks between the two via env var ``ADMISSIBILITY_REGIME``:
  * ``incremental`` (default): fast path for in-loop use.
  * ``shacl``:                 real pySHACL. Slower; used by experiments that
                               want to pay the cost to get the specification
                               validator.

The differential suites compare the implementations on their tested inputs.
These finite campaigns do not prove equivalence on arbitrary RDF graphs.
"""
from __future__ import annotations

import os
import hashlib
import math
import re
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from typing import Iterable, List, Optional, Set, Tuple

from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import RDF, XSD

EX = Namespace("http://example.org/building#")
ALLOWED_VENTILATION = {"off", "normal", "high", "emergency"}

_DEFAULT_SHAPES_PATH = os.environ.get("SHAPES_PATH", "shapes/invariants.ttl")
_SHIPPED_SHAPES_PATH = str(Path(__file__).resolve().parent.parent / "shapes/invariants.ttl")
_ZONE_PROPERTIES = (EX.currentSetpoint, EX.ventilationMode, EX.emergencyState)
_DECIMAL_LEXICAL = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)")
_INTEGER_LEXICAL = re.compile(r"[+-]?[0-9]+")

# This hand-written validator implements exactly this shipped shape file.
# Local scope alone is insufficient to justify using it for other shapes.
_COMPILED_SHAPES_SHA256 = "1ca542668f976b88b2c8c9587d496ab0b3e3841dc0f742e9e72be9bde5c00301"

def supports_incremental_shapes(shapes_path: Optional[str] = None) -> bool:
    path = Path(shapes_path or _DEFAULT_SHAPES_PATH)
    if not path.is_absolute() and not path.exists():
        path = Path(__file__).resolve().parent.parent / path
    # Universal newline conversion keeps Windows CRLF checkouts on the same
    # compiled profile as the repository's LF source.
    return hashlib.sha256(path.read_text(encoding="utf-8").encode("utf-8")).hexdigest() == _COMPILED_SHAPES_SHA256


def _is_decimal(value) -> bool:
    from decimal import Decimal
    return isinstance(value, Decimal)


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

def _shapes_bytes(shapes_path: str) -> bytes:
    path = Path(shapes_path)
    if not path.is_absolute() and not path.exists():
        path = Path(__file__).resolve().parent.parent / path
    return path.read_bytes()


@lru_cache(maxsize=8)
def _parse_shapes(content_sha256: str, content: bytes) -> Graph:
    g = Graph()
    g.parse(data=content.decode("utf-8"), format="turtle")
    return g


def _load_shapes_graph(shapes_path: str) -> Graph:
    """
    Load the shapes graph, cached by CONTENT identity (not by path): a file
    edited in place after a first load is re-parsed, never served stale.
    We read the bytes ourselves and hand them to rdflib via ``data=`` so
    Windows absolute paths are never interpreted as URL schemes.
    """
    content = _shapes_bytes(shapes_path)
    return _parse_shapes(hashlib.sha256(content).hexdigest(), content)


def _unused_load_shapes_graph(shapes_path: str) -> Graph:  # pragma: no cover (legacy body kept for reference)
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

    try:
        conforms, _results_graph, results_text = validate(
            data_graph=graph,
            shacl_graph=shapes_graph,
            inference=None,
            advanced=True,        # enables SPARQL-based constraints
            debug=False,
        )
    except InvalidOperation:
        # pySHACL/RDFLib can raise while comparing Decimal NaN. A validator
        # error does not establish admissibility: refuse the candidate and
        # retain an explicit diagnostic, rather than accepting or crashing.
        return False, "Reference validation error: numeric comparison raised decimal.InvalidOperation; candidate refused"
    return bool(conforms), results_text


# ---------------------------------------------------------------------------
# Fast path: zone-scoped hand-coded equivalent of the SHACL shapes
# ---------------------------------------------------------------------------

def _zone_class_closure(graph: Graph) -> Set[URIRef]:
    """ex:HVAC_Zone and every class declared (transitively) as its subclass in
    the data graph, as SHACL class targeting requires."""
    from rdflib.namespace import RDFS
    return set(graph.transitive_subjects(RDFS.subClassOf, EX.HVAC_Zone))


def _fast_literal_eligible(predicate: URIRef, value) -> bool:
    """Representations supported by the compiled checker, not a validity test.

    Unsupported representations are sent to pySHACL, never normalized here.
    Invalid cardinalities, out-of-range numbers and unknown plain-string
    ventilation modes stay in the fragment so the fast checker can reject them.
    """
    if not isinstance(value, Literal) or value.language is not None or value.ill_typed:
        return False
    if predicate == EX.emergencyState:
        return (value.datatype == XSD.boolean and str(value) in {"true", "false"}
                and isinstance(value.toPython(), bool))
    if predicate == EX.ventilationMode:
        return value.datatype is None and isinstance(value.toPython(), str)
    if predicate == EX.currentSetpoint:
        lexical = str(value)
        if value.datatype == XSD.decimal:
            if _DECIMAL_LEXICAL.fullmatch(lexical) is None:
                return False
            py = value.toPython()
            # RDFLib can cache a Python float/int when that object is passed
            # directly to Literal(..., datatype=XSD.decimal). Require its
            # decimal representation to match the RDF lexical value. All
            # bounds in this compiled shape file are exactly representable
            # integers, so this also preserves their comparisons.
            return ((isinstance(py, Decimal) and py.is_finite() and py == Decimal(lexical))
                    or (type(py) is int and Decimal(py) == Decimal(lexical))
                    or (type(py) is float and math.isfinite(py)
                        and Decimal(str(py)) == Decimal(lexical)))
        if value.datatype == XSD.integer:
            return (type(value.toPython()) is int
                    and _INTEGER_LEXICAL.fullmatch(lexical) is not None)
    return False


def fast_path_eligible(graph: Graph) -> Tuple[bool, str]:
    """
    Conservative input profile for the compiled checker of the shipped shapes:

      (a) no class is declared a subclass of ex:HVAC_Zone in the data graph;
      (b) every subject carrying ex:currentSetpoint, ex:ventilationMode or
          ex:emergencyState is directly typed ex:HVAC_Zone;
      (c) setpoints are finite, well-formed xsd:decimal or xsd:integer literals;
          ventilation modes are plain literals without a language tag;
          emergency states are canonical xsd:boolean "true" or "false".

    Any other graph is validated by the reference pySHACL validator instead.
    Agreement is tested on the reported cases, not proved by this predicate.
    The incremental resolver checks every proposed write for preservation of
    this profile; a generic single-slot rewrite need not preserve it.
    """
    if _zone_class_closure(graph) - {EX.HVAC_Zone}:
        return False, "subclass declarations of ex:HVAC_Zone are outside the fast-path fragment"
    typed = set(graph.subjects(RDF.type, EX.HVAC_Zone))
    for prop in _ZONE_PROPERTIES:
        for subject, value in graph.subject_objects(prop):
            if subject not in typed:
                return False, f"{_fmt(subject)} carries zone properties without a direct ex:HVAC_Zone type"
            if not _fast_literal_eligible(prop, value):
                return False, f"{_fmt(subject)} has an unsupported literal representation for {_fmt(prop)}"
    return True, "eligible"


def fast_path_preserved_by_write(graph: Graph, subject: URIRef,
                                 predicate: URIRef, value: Literal) -> bool:
    """Sufficient local preservation check, assuming an eligible input graph.

    Rewrites can change only the three constrained slots of an already typed
    zone, using a supported literal. Schema, typing and other writes fall back
    to reference validation. Deleting all old slot values is permitted.
    """
    return (predicate in _ZONE_PROPERTIES
            and (subject, RDF.type, EX.HVAC_Zone) in graph
            and _fast_literal_eligible(predicate, value))


def get_admissibility_regime() -> str:
    """One configuration policy shared by both resolvers and the dispatcher."""
    regime = os.environ.get("ADMISSIBILITY_REGIME", "incremental").lower()
    if regime not in {"shacl", "incremental"}:
        raise ValueError(f"Unknown admissibility regime: {regime}")
    return regime


def select_admissibility_backend(graph: Graph, shapes_path: Optional[str] = None) -> str:
    if (get_admissibility_regime() == "shacl"
            or not supports_incremental_shapes(shapes_path)
            or not fast_path_eligible(graph)[0]):
        return "shacl"
    return "incremental"


def check_admissibility_incremental(
    graph: Graph,
    focus_zones: Optional[Iterable[URIRef]] = None,
) -> Tuple[bool, str]:
    """Guarded checker for the shipped shapes, with full pySHACL fallback.

    No RDF terms are normalized. A focused check assumes that the caller has
    established admissibility of the untouched zones. The resolver establishes
    that precondition once, then uses the private kernel after checking each
    write, avoiding a repeated global eligibility scan.
    """
    if (not supports_incremental_shapes(_SHIPPED_SHAPES_PATH)
            or not fast_path_eligible(graph)[0]):
        return check_admissibility_shacl(graph, _SHIPPED_SHAPES_PATH)
    return _check_admissibility_fast(graph, focus_zones)


def _check_admissibility_fast(
    graph: Graph,
    focus_zones: Optional[Iterable[URIRef]] = None,
) -> Tuple[bool, str]:
    """
    Private compiled kernel. Caller must check the shape and literal profile.

    If ``focus_zones`` is given, only those zones are checked (safe because
    every shape in that file is zone-local). Otherwise every subject typed
    HVAC_Zone is checked, and ex:ZoneA is always checked for its two
    node-targeted shapes, whether or not it carries the class assertion
    (sh:targetNode does not depend on rdf:type).

    Admitted integer/decimal values use exact decimal comparison. Float/double,
    non-finite and malformed literals must be handled by the reference path.
    """
    violations: List[str] = []
    zone_classes = _zone_class_closure(graph)
    if focus_zones is None:
        typed_zones = {z for c in zone_classes for z in graph.subjects(RDF.type, c)}
        zones_iter = sorted(typed_zones | {EX.ZoneA}, key=str)
    else:
        zones_iter = sorted(set(focus_zones), key=str)

    for zone in zones_iter:
        class_targeted = any((zone, RDF.type, cls) in graph for cls in zone_classes)
        setpoints = list(graph.objects(zone, EX.currentSetpoint))
        sp_value: Optional[Decimal] = None
        if len(setpoints) == 1:
            raw_sp = setpoints[0]
            py = raw_sp.toPython() if hasattr(raw_sp, "toPython") else raw_sp
            numeric = (not isinstance(py, bool)) and (isinstance(py, (int, float)) or _is_decimal(py))
            if numeric:
                try:
                    sp_value = Decimal(str(raw_sp))
                except (InvalidOperation, ValueError):
                    sp_value = None
                    numeric = False
                if sp_value is not None and not sp_value.is_finite():
                    sp_value = None
                    numeric = False
                    if class_targeted:
                        violations.append(f"ZoneSetpointShape: {_fmt(zone)} setpoint not finite")
            if class_targeted and not numeric and sp_value is None and "not finite" not in " ".join(violations[-1:]):
                violations.append(f"ZoneSetpointShape: {_fmt(zone)} setpoint not numeric")
        if class_targeted:
            # --- ZoneSetpointShape: exactly 1 value in [18, 26] ---
            if len(setpoints) != 1:
                violations.append(
                    f"ZoneSetpointShape: {_fmt(zone)} currentSetpoint cardinality != 1 (found {len(setpoints)})"
                )
            elif sp_value is not None:
                if sp_value < Decimal("18"):
                    violations.append(f"ZoneSetpointShape: {_fmt(zone)} setpoint {sp_value} < 18.0")
                if sp_value > Decimal("26"):
                    violations.append(f"ZoneSetpointShape: {_fmt(zone)} setpoint {sp_value} > 26.0")

        # --- ZoneVentilationShape: exactly 1 value in the enumeration ---
        modes = list(graph.objects(zone, EX.ventilationMode))
        mode_value: Optional[str] = None
        if class_targeted:
            if len(modes) != 1:
                violations.append(f"ZoneVentilationShape: {_fmt(zone)} ventilationMode cardinality != 1")
            elif modes[0] not in {Literal(mode) for mode in ALLOWED_VENTILATION}:
                violations.append(
                    f'ZoneVentilationShape: {_fmt(zone)} ventilationMode "{modes[0]}" not in allowed set'
                )
        if len(modes) == 1:
            mode_value = str(modes[0].toPython()) if modes[0] in {Literal(m) for m in ALLOWED_VENTILATION} else str(modes[0])

        # --- EmergencyStateShape: exactly 1 boolean ---
        estates = list(graph.objects(zone, EX.emergencyState))
        estate_value = None
        if class_targeted:
            if len(estates) != 1:
                violations.append(f"EmergencyStateShape: {_fmt(zone)} emergencyState cardinality != 1")
            else:
                py = estates[0].toPython() if hasattr(estates[0], "toPython") else estates[0]
                if not isinstance(py, bool):
                    violations.append(f"EmergencyStateShape: {_fmt(zone)} emergencyState not boolean")
                else:
                    estate_value = py
            # --- EmergencyVentLockShape: emergencyState true => vent="emergency" ---
            if estate_value is True and mode_value is not None and mode_value != "emergency":
                violations.append(
                    f'EmergencyVentLockShape: {_fmt(zone)} emergencyState=true but vent="{mode_value}"'
                )

        if zone == EX.ZoneA:
            # Node-targeted shapes examine EVERY value node of the property.
            all_values = []
            for lit in setpoints:
                try:
                    dec = Decimal(str(lit))
                    py = lit.toPython() if hasattr(lit, "toPython") else lit
                    ok = (not isinstance(py, bool)) and (isinstance(py, (int, float)) or _is_decimal(py)) and dec.is_finite()
                except (InvalidOperation, ValueError):
                    ok = False
                all_values.append(dec if ok else None)
            # --- ZoneAComfortCapShape (sh:targetNode ex:ZoneA): every setpoint <= 23 ---
            for dec in all_values:
                if dec is None:
                    violations.append(f"ZoneAComfortCapShape: {_fmt(zone)} setpoint not comparable with 23.0")
                elif dec > Decimal("23"):
                    violations.append(f"ZoneAComfortCapShape: {_fmt(zone)} setpoint {dec} > 23.0 ZoneA cap")
            # --- ZoneAEmergencyVentShape: if any ventilation value is "off", every setpoint <= 21 ---
            if any(str(m) == "off" for m in modes):
                for dec in all_values:
                    if dec is not None and dec > Decimal("21"):
                        violations.append(
                            f'ZoneAEmergencyVentShape: {_fmt(zone)} vent="off" requires setpoint <= 21.0, found {dec}'
                        )

    if violations:
        return False, "\n".join(violations)
    return True, "conforms"


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
    if select_admissibility_backend(graph, shapes_path) == "shacl":
        return check_admissibility_shacl(graph, shapes_path)
    return _check_admissibility_fast(graph)


if __name__ == "__main__":
    g = Graph()
    g.parse("data/base_graph.ttl", format="turtle")

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
