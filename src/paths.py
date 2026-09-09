"""
Single source of truth for where results are written.

Every script writes below <repository>/results/ regardless of the directory it
is started from:

    results/hvac/          result files of the building-automation scenario (all tables)
    results/hvac/traces/   recorded traces of that scenario (pipeline, role contexts,
                           multi-window steps, stress, governance-clean)
    results/ev/            result file of the EV-charging scenario
    results/ev/traces/     recorded traces of that scenario
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "results"
HVAC = RESULTS / "hvac"
HVAC_TRACES = HVAC / "traces"
EV = RESULTS / "ev"
EV_TRACES = EV / "traces"


def ensure_dirs() -> None:
    for directory in (HVAC, HVAC_TRACES, EV, EV_TRACES):
        directory.mkdir(parents=True, exist_ok=True)


def hvac(name: str) -> str:
    """Absolute path of a result file of the building scenario."""
    ensure_dirs()
    return str(HVAC / name)


def hvac_trace(name: str) -> str:
    ensure_dirs()
    return str(HVAC_TRACES / name)


def ev(name: str) -> str:
    ensure_dirs()
    return str(EV / name)


def ev_trace(name: str) -> str:
    ensure_dirs()
    return str(EV_TRACES / name)


def relative(path) -> str:
    """Repository-relative form for messages."""
    try:
        return str(Path(path).resolve().relative_to(ROOT)).replace("\\", "/")
    except ValueError:
        return str(path)
