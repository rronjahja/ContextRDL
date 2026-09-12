"""Verify a freshly regenerated campaign without changing any result files.

Run after scripts/regenerate.py --fresh. Result checksums normalize CRLF to LF,
so the same checksums verify on Windows and on clones using LF line endings.
Full HVAC traces must embed the current canonical rules, including their role
conditions; replay of an old embedded rule set is not sufficient for this check.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rule_loader import load_rules
from trace import _environment, file_sha256
from regenerate import CORRECTNESS, SCALING, RESULT_SHA256_PROFILE

REQUIRED_TRACES = (
    "trace_default.json", "trace_tie_conflict.json",
    "trace_governance_op_gt_occ.json", "trace_governance_occ_gt_op.json",
)


def verify_campaign(root: Path, source_fingerprint: str) -> list[str]:
    """Return concrete failures; the caller supplies the local source fingerprint."""
    failures = []
    manifest_path = root / "results/regeneration.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("overall_pass") is not True:
        failures.append("The regeneration campaign did not complete successfully")
    expected = [(s, "shacl") for s in CORRECTNESS] + [(s, "incremental") for s in SCALING]
    commands = manifest.get("commands", [])
    actual = [(c.get("script"), c.get("admissibility_regime")) for c in commands]
    if actual != expected or any(c.get("exit_code") != 0 for c in commands):
        failures.append("The manifest does not contain every required successful command in order")
    if manifest.get("environment", {}).get("source_manifest_sha256") != source_fingerprint:
        failures.append("Local source content differs from the regenerated campaign")
    if manifest.get("pythonhashseed") != "0":
        failures.append("The campaign does not record PYTHONHASHSEED=0")
    if manifest.get("result_sha256_profile") != RESULT_SHA256_PROFILE:
        failures.append("Legacy or unknown checksum profile; run a new complete regeneration")
        return failures

    hashes = manifest.get("result_sha256", {})
    if not isinstance(hashes, dict) or not hashes:
        return failures + ["No result-file checksums are recorded"]
    root = root.resolve()
    for name, expected_hash in hashes.items():
        path = (root / name).resolve()
        if not path.is_relative_to(root / "results") or path == manifest_path.resolve():
            failures.append("Invalid result-file path: " + name)
        elif not path.is_file():
            failures.append("Missing result file: " + name)
        elif file_sha256(str(path)) != expected_hash:
            failures.append("Result checksum mismatch: " + name)

    expected_rules = sorted(load_rules(str(root / "configs/rules.json")), key=lambda r: r["rid"])
    trace_dir = root / "results/hvac/traces"
    for name in REQUIRED_TRACES:
        path = trace_dir / name
        if path.relative_to(root).as_posix() not in hashes:
            failures.append("Required trace is absent from the checksum manifest: " + name)
    for path in sorted(trace_dir.glob("*.json")):
        trace = json.loads(path.read_text(encoding="utf-8"))
        if "enabled_actions" not in trace:
            continue  # Action-based traces have a different, explicitly limited replay boundary.
        inline = trace.get("settings", {}).get("dependencies", {}).get("rules_inline")
        if inline != expected_rules:
            failures.append("Full trace does not embed the current canonical rules: " + path.name)
        if trace.get("environment", {}).get("source_manifest_sha256") != source_fingerprint:
            failures.append("Full trace records different source content: " + path.name)
        if path.relative_to(root).as_posix() not in hashes:
            failures.append("Full trace is absent from the checksum manifest: " + path.name)
    return failures


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    try:
        fingerprint = _environment()["source_manifest_sha256"]
        failures = verify_campaign(ROOT, fingerprint)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise SystemExit("Verification failed: " + str(exc)) from exc
    if failures:
        raise SystemExit("Verification failed:\n" + "\n".join("- " + item for item in failures))
    print("PASS: complete campaign, portable checksums, source fingerprint, and current rules in full HVAC traces")


if __name__ == "__main__":
    main()
