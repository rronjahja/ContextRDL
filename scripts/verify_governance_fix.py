"""Verify the governance correction without re-running the timing campaign.

Run before any --fresh regeneration:
    python scripts/verify_governance_fix.py

Writes results/governance_fix_verification.json and separate verification logs.
The existing experimental JSON files and recorded traces are preserved.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from trace import _environment

SCRIPTS = (
    "src/test_governance_enablement.py",
    "src/test_review_regressions.py",
    "src/test_v5_regressions.py",
)


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    if sys.flags.optimize:
        raise SystemExit("Run without -O: regression assertions must remain enabled")
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    results = ROOT / "results"
    manifest = results / "regeneration.json"
    if not manifest.is_file():
        raise SystemExit("Keep the published results/ archive in place and run this verification before --fresh.")
    report_path = results / "governance_fix_verification.json"
    before = {p.relative_to(ROOT).as_posix(): sha256(p)
              for p in results.rglob("*.json") if p != report_path}
    logs = results / "governance_fix_logs"
    logs.mkdir(parents=True, exist_ok=True)
    report = {
        "purpose": "Governance correctness and compatibility checks; no timing benchmarks",
        "started_utc": datetime.now(timezone.utc).isoformat(),
        "environment": _environment(),
        "archived_campaign_manifest_sha256": sha256(manifest),
        "archive_traces_override": os.environ.get("CONTEXTRDL_ARCHIVE_TRACES"),
        "commands": [],
        "overall_pass": False,
    }
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    env = dict(os.environ, ADMISSIBILITY_REGIME="shacl", PYTHONHASHSEED="0",
               PYTHONIOENCODING="utf-8", PYTHONUTF8="1",
               PYTHONDONTWRITEBYTECODE="1", PYTHONUNBUFFERED="1")
    for script in SCRIPTS:
        print("Running " + script, flush=True)
        log_path = logs / (Path(script).stem + ".log")
        start = time.perf_counter()
        with log_path.open("w", encoding="utf-8") as log:
            result = subprocess.run([sys.executable, "-X", "utf8", script],
                                    cwd=ROOT, env=env, stdout=log,
                                    stderr=subprocess.STDOUT)
        report["commands"].append({
            "script": script, "exit_code": result.returncode,
            "elapsed_seconds": time.perf_counter() - start,
            "log": log_path.relative_to(ROOT).as_posix(),
        })
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        if result.returncode:
            print(log_path.read_text(encoding="utf-8")[-6000:])
            break
    after = {p.relative_to(ROOT).as_posix(): sha256(p)
             for p in results.rglob("*.json") if p != report_path}
    report["archived_results_unchanged"] = before == after
    report["overall_pass"] = (
        len(report["commands"]) == len(SCRIPTS)
        and all(c["exit_code"] == 0 for c in report["commands"])
        and report["archived_results_unchanged"]
    )
    report["finished_utc"] = datetime.now(timezone.utc).isoformat()
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print("Verification " + ("PASS" if report["overall_pass"] else "FAIL"))
    print("Report: " + str(report_path))
    if not report["overall_pass"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
