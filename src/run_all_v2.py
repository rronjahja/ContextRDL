"""
Orchestrator.  Runs the two equivalence tests first, aborts on failure,
then runs the five experiments.  Writes a single consolidated JSON summary
the user can share back.

Order matters:
  1. test_validator_equivalence   (pySHACL vs incremental validator)
  2. test_resolver_equivalence    (original vs incremental resolver)
     -- if either fails, do NOT run the experiments; the incremental path
        is untrustworthy and results would be misleading.
  3. experiment_pyshacl_baseline  (real baseline: ours vs shacl-gated vs posthoc)
  4. experiment_determinism_stress (ours vs random-shuffle, varying N)
  5. experiment_scalability_v2    (original vs incremental resolver runtime)
  6. experiment_governance_v2     (admissibility actually fires; governance is cleaner)
  7. experiment_trace_integrity   (full replay of recorded + fresh traces)

Each step writes its own JSON under results/ ; this script additionally
writes results/experiments_v2_summary.json pulling the key numbers together.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from typing import Any, Dict


RESULTS_DIR = "results"


def _run(cmd: list, cwd: str = ".") -> Dict[str, Any]:
    t0 = time.perf_counter()
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    elapsed = time.perf_counter() - t0
    return {
        "cmd":          cmd,
        "returncode":   proc.returncode,
        "stdout":       proc.stdout,
        "stderr":       proc.stderr,
        "elapsed_s":    round(elapsed, 3),
    }


def _read_json(path: str) -> Any:
    if not os.path.exists(path):
        return {"__missing__": path}
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def main():
    os.makedirs(RESULTS_DIR, exist_ok=True)
    log: Dict[str, Any] = {"steps": []}

    # ---- Step 1: validator equivalence ----
    print(">>> [1/7] test_validator_equivalence ...")
    step = _run([sys.executable, "test_validator_equivalence.py"])
    log["steps"].append({"name": "test_validator_equivalence", **step})
    print(step["stdout"])
    if step["stderr"]:
        print("STDERR:", step["stderr"])
    if step["returncode"] != 0:
        print("ABORT: validator equivalence test failed. Fix admissibility.py before continuing.")
        _finalise(log)
        sys.exit(1)

    # ---- Step 2: resolver equivalence ----
    print("\n>>> [2/7] test_resolver_equivalence ...")
    step = _run([sys.executable, "test_resolver_equivalence.py"])
    log["steps"].append({"name": "test_resolver_equivalence", **step})
    print(step["stdout"])
    if step["stderr"]:
        print("STDERR:", step["stderr"])
    if step["returncode"] != 0:
        print("ABORT: resolver equivalence test failed. Fix resolver_incremental.py before continuing.")
        _finalise(log)
        sys.exit(1)

    # ---- Step 3: pySHACL baseline ----
    print("\n>>> [3/7] experiment_pyshacl_baseline ...")
    step = _run([sys.executable, "experiment_pyshacl_baseline.py"])
    log["steps"].append({"name": "experiment_pyshacl_baseline", **step})
    print(step["stdout"][-4000:])  # baseline can be chatty

    # ---- Step 4: determinism stress ----
    print("\n>>> [4/7] experiment_determinism_stress ...")
    step = _run([sys.executable, "experiment_determinism_stress.py"])
    log["steps"].append({"name": "experiment_determinism_stress", **step})
    print(step["stdout"])

    # ---- Step 5: scalability v2 ----
    print("\n>>> [5/7] experiment_scalability_v2 ...")
    step = _run([sys.executable, "experiment_scalability_v2.py"])
    log["steps"].append({"name": "experiment_scalability_v2", **step})
    print(step["stdout"])

    # ---- Step 6: governance v2 ----
    print("\n>>> [6/7] experiment_governance_v2 ...")
    step = _run([sys.executable, "experiment_governance_v2.py"])
    log["steps"].append({"name": "experiment_governance_v2", **step})
    print(step["stdout"])

    # ---- Step 7: trace integrity ----
    print("\n>>> [7/7] experiment_trace_integrity ...")
    step = _run([sys.executable, "experiment_trace_integrity.py"])
    log["steps"].append({"name": "experiment_trace_integrity", **step})
    print(step["stdout"])

    _finalise(log)
    print(f"\nDONE. See {RESULTS_DIR}/experiments_v2_summary.json")


def _finalise(log: Dict[str, Any]) -> None:
    summary = {
        "run_log":        log,
        "test_validator_equivalence":    _read_json(f"{RESULTS_DIR}/test_validator_equivalence.json"),
        "test_resolver_equivalence":     _read_json(f"{RESULTS_DIR}/test_resolver_equivalence.json"),
        "experiment_pyshacl_baseline":   _read_json(f"{RESULTS_DIR}/experiment_pyshacl_baseline.json"),
        "experiment_determinism_stress": _read_json(f"{RESULTS_DIR}/experiment_determinism_stress.json"),
        "experiment_scalability_v2":     _read_json(f"{RESULTS_DIR}/experiment_scalability_v2.json"),
        "experiment_governance_v2":      _read_json(f"{RESULTS_DIR}/experiment_governance_v2.json"),
        "experiment_trace_integrity":    _read_json(f"{RESULTS_DIR}/experiment_trace_integrity.json"),
    }
    out_path = f"{RESULTS_DIR}/experiments_v2_summary.json"
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2)


if __name__ == "__main__":
    main()
