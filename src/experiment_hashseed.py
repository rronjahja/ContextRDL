"""
Cross-process determinism under Python hash randomization.

Thirty trials inside one interpreter share one hash seed, so they cannot
detect a dependence on set/dict iteration order by themselves. This script
runs the default evaluation step in separate interpreter processes with
different PYTHONHASHSEED values and checks that the successor digest and the
decision list are identical in every process.

Run from the project root:
    python src/experiment_hashseed.py
Writes results/hvac/experiment_hashseed.json
"""
from __future__ import annotations

import paths  # noqa: E402  (results layout)

import json
import os
import pathlib
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent if (HERE / "rule_engine.py").exists() else HERE
SEEDS = ["0", "1", "7", "42", "12345", "random", "random"]

CHILD = r"""
import json, sys
sys.path.insert(0, %r)
from engine import run_engine
_, _, _, _, trace = run_engine(save_trace_file=False)
print(json.dumps({"digest": trace["successor_graph"]["digest"],
                  "decisions": [(d["rid"], d["accepted"], d["reason"]) for d in trace["decisions"]]}))
"""


def main():
    rows = []
    for seed in SEEDS:
        env = dict(os.environ, PYTHONHASHSEED=seed, PYTHONDONTWRITEBYTECODE="1")
        proc = subprocess.run([sys.executable, "-c", CHILD % str(HERE)], cwd=str(PROJECT_ROOT),
                              env=env, capture_output=True, text=True, check=True)
        out = json.loads(proc.stdout.strip().splitlines()[-1])
        rows.append({"PYTHONHASHSEED": seed, "digest": out["digest"], "decisions": out["decisions"]})
        print(f"PYTHONHASHSEED={seed:<7} digest={out['digest'][:16]}...")
    digests = {r["digest"] for r in rows}
    decisions = {json.dumps(r["decisions"]) for r in rows}
    result = {"processes": len(rows), "unique_digests": len(digests),
              "unique_decision_lists": len(decisions), "rows": rows}
    pathlib.Path(paths.hvac("experiment_hashseed.json")).write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"{len(rows)} processes, {len(digests)} unique digest(s), {len(decisions)} unique decision list(s)")
    if len(digests) != 1 or len(decisions) != 1:
        raise SystemExit("cross-process disagreement")


if __name__ == "__main__":
    main()
