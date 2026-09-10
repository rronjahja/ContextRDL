"""Numeric representation fixed by the execution configuration.

JSON decimal tokens are read without a binary-float intermediate. Decimal
strings are also accepted by the Python API and in recorded actions. For an
already-created Python float, its shortest decimal string is the input value;
digits lost before this API was called cannot be recovered. Booleans and
non-finite numbers are not numeric commands.
"""
from __future__ import annotations

from decimal import Context, Decimal, InvalidOperation, MAX_EMAX, MIN_EMIN, localcontext
import re
from typing import Any

from rdflib import Graph, Literal
from rdflib.namespace import XSD

NUMERIC_PROFILE = "finite-decimal-1"
_NUMBER = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")


def decimal_value(value: Any) -> Decimal:
    """Interpret the supplied decimal value exactly; never round through float."""
    if isinstance(value, Literal):
        if value.language is not None or value.ill_typed:
            raise ValueError("Expected a finite numeric value")
        value = str(value)
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError("Expected a finite numeric value")
    text = str(value)
    if _NUMBER.fullmatch(text) is None:
        raise ValueError("Expected a finite numeric value")
    try:
        result = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError("Expected a finite numeric value") from exc
    if not result.is_finite():
        raise ValueError("Expected a finite numeric value")
    return result


def decimal_text(value: Any) -> str:
    """Fixed-point lexical form: at least one fractional digit, no redundant zeros."""
    number = decimal_value(value)
    if number.is_zero():
        return "0.0"
    text = format(number, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text if "." in text else text + ".0"


def decimal_literal(value: Any) -> Literal:
    return Literal(decimal_text(value), datatype=XSD.decimal, normalize=False)


def _sum_context(values: list[Decimal]) -> Context:
    # Enough coefficient digits for alignment and carry in the complete sum.
    # Starting from a new Context avoids dependence on the caller's precision.
    high = max([0] + [v.adjusted() for v in values if v])
    low = min([0] + [v.as_tuple().exponent for v in values])
    return Context(prec=high - low + len(str(max(1, len(values)))) + 2,
                   Emax=MAX_EMAX, Emin=MIN_EMIN)


def decimal_add(left: Any, right: Any) -> Decimal:
    values = [decimal_value(left), decimal_value(right)]
    with localcontext(_sum_context(values)):
        return values[0] + values[1]


def validation_decimal_context(graph: Graph) -> Context:
    """Exact decimal SUM for the shipped EV budget query.

This bound covers sums of the graph's decimal/integer literals. It is not a
precision guarantee for arbitrary user-supplied SPARQL arithmetic functions.
Malformed values remain for SHACL to reject.
"""
    values = []
    for _, _, value in graph:
        if isinstance(value, Literal) and value.datatype in (XSD.decimal, XSD.integer):
            try:
                values.append(decimal_value(value))
            except ValueError:
                pass
    return _sum_context(values)
