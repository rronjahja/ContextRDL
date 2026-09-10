"""Regenerate the complete campaign sequentially with explicit validator regimes.

Run from any directory: python scripts/regenerate.py
An optional --start-at filename.py resumes after a failed command, preserving
earlier manifest entries and requiring the same source fingerprint.
Use --fresh for a new campaign: existing results are moved to a dated backup.
"""
from __future__ import annotations

import argparse
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

CORRECTNESS = [
    "src/test_review_regressions.py", "src/test_v5_regressions.py",
    "src/engine.py", "src/test_validator_equivalence.py",
    "src/test_validator_differential.py", "src/test_resolver_equivalence.py",
    "src/experiment_policy_profile.py", "src/experiment_semantic_boundaries.py",
    "src/experiment_hvac_v3.py",
    "src/experiment_runtime_controlled.py", "src/experiment_determinism_stress.py",
    "src/experiment_hashseed.py", "src/experiment_governance_v2.py",
    "src/experiment_role_filter.py", "src/experiment_invalid_start.py",
    "ev/experiment_ev.py", "ev/experiment_operational.py",
    "src/experiment_multiwindow.py", "src/experiment_replay_extra.py",
    "src/experiment_cross_implementation.py", "src/experiment_atomic_commit.py",
    "src/experiment_restart_delivery.py",
]
SCALING = ["src/experiment_scalability_v2.py", "src/experiment_scalability_sd.py",
           "src/experiment_scalability_profile.py"]


def portable(value):
    """Normalize report paths, never RDF terms or recorded graph snapshots."""
    if isinstance(value, dict):
        return {k: portable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [portable(v) for v in value]
    if isinstance(value, str) and value.startswith(str(ROOT) + os.sep):
        return Path(value).relative_to(ROOT).as_posix()
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-at", help="Resume at this script filename")
    parser.add_argument("--fresh", action="store_true",
                        help="Move existing results to a dated backup before running")
    args = parser.parse_args()
    if sys.flags.optimize:
        raise SystemExit("Run without -O: experiment assertions must remain enabled")
    if args.fresh and args.start_at:
        raise SystemExit("--fresh and --start-at cannot be combined")
    out = ROOT / "results"
    if args.fresh and out.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup = ROOT / ("results_previous_" + stamp)
        out.rename(backup)
        print("Previous results moved to " + backup.name, flush=True)
    log_dir = out / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = out / "regeneration.json"
    env_info = _environment()
    commands = [(script, "shacl") for script in CORRECTNESS] + [
        (script, "incremental") for script in SCALING]
    names = [Path(script).name for script, _ in commands]
    if args.start_at and args.start_at not in names:
        raise SystemExit("Unknown script: " + args.start_at)
    start = names.index(args.start_at) if args.start_at else 0
    manifest = {"started_utc": datetime.now(timezone.utc).isoformat(),
                "environment": env_info, "commands": [], "overall_pass": False}
    if start:
        previous = json.loads(manifest_path.read_text(encoding="utf-8"))
        if previous["environment"]["source_manifest_sha256"] != env_info["source_manifest_sha256"]:
            raise SystemExit("Source changed; start a complete regeneration")
        manifest["commands"] = previous["commands"][:start]
        if len(manifest["commands"]) != start or any(r["exit_code"] for r in manifest["commands"]):
            raise SystemExit("Earlier commands have not all passed")
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    for script, regime in commands[start:]:
        print(f"Running {script} (ADMISSIBILITY_REGIME={regime})", flush=True)
        env = dict(os.environ, ADMISSIBILITY_REGIME=regime, PYTHONHASHSEED="0",
                   PYTHONDONTWRITEBYTECODE="1", PYTHONUNBUFFERED="1")
        log = log_dir / (Path(script).stem + ".log")
        begin = time.perf_counter()
        with log.open("w", encoding="utf-8") as stream:
            result = subprocess.run([sys.executable, script], cwd=ROOT, env=env,
                                    stdout=stream, stderr=subprocess.STDOUT)
        manifest["commands"].append({"script": script, "admissibility_regime": regime,
            "exit_code": result.returncode, "elapsed_seconds": time.perf_counter() - begin,
            "log": log.relative_to(ROOT).as_posix()})
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        if result.returncode:
            print(log.read_text(encoding="utf-8")[-6000:])
            raise SystemExit(f"Failed: {script}; see {log.relative_to(ROOT)}")
    for path in sorted(out.rglob("*.json")):
        if path == manifest_path:
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        changed = portable(data)
        if isinstance(changed, dict) and path.parent.name != "traces":
            regime = "incremental" if path.stem.startswith("experiment_scalability") else "shacl"
            changed["campaign"] = {"environment": env_info, "requested_admissibility_regime": regime,
                "note": "Validator comparison suites also explicitly invoke both backends; per-case fields specify them."}
        if changed != data:
            path.write_text(json.dumps(changed, indent=2, ensure_ascii=False), encoding="utf-8")
    manifest["overall_pass"] = True
    manifest["finished_utc"] = datetime.now(timezone.utc).isoformat()
    manifest["result_sha256"] = {
        p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(out.rglob("*.json")) if p != manifest_path}
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"All {len(commands)} commands passed; results/regeneration.json records provenance.")


if __name__ == "__main__":
    main()
