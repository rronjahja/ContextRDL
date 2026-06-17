"""
Scalability with standard deviations.

The existing experiment_scalability_v2.py runs each N once, so it cannot
report the mean +/- sd the manuscript's Table needs. This harness repeats
each N several times and reports mean and standard deviation for both
resolvers, plus the speedup computed from the means.

It reuses run_case() from experiment_scalability_v2 so the workload is
IDENTICAL to the one already described in the paper (N zones, N PreheatRequest
events, distinct targets, nothing shadowed).

Run from the project root:
    python src/experiment_scalability_sd.py
(or from inside src/:  python experiment_scalability_sd.py)

By default it does 5 repeats per N. The large N values (400, 800) use the
reference resolver, which is the O(N^2) one, so 5 repeats at N=800 can take
a few minutes. Lower REPEATS to 3 if you want it faster; the means are stable.

Writes results/experiment_scalability_sd.json and prints a summary plus the
ready-to-paste LaTeX rows.
"""
from __future__ import annotations

import json
import os
import statistics
import sys
from pathlib import Path

# Make project imports work whether run from root or from src/
HERE = Path(__file__).resolve().parent
if (HERE / "experiment_scalability_v2.py").exists():
    sys.path.insert(0, str(HERE))
    PROJECT_ROOT = HERE.parent
else:
    PROJECT_ROOT = HERE
os.chdir(PROJECT_ROOT)

from experiment_scalability_v2 import run_case  # noqa: E402

SIZES = [10, 50, 100, 200, 400, 800]
REPEATS = 5


def main():
    os.makedirs("results", exist_ok=True)
    rows = []

    for n in SIZES:
        orig_times, new_times = [], []
        accepted = None
        digest_ok = True
        for _ in range(REPEATS):
            r = run_case(n)
            orig_times.append(r["time_seconds_orig"])
            new_times.append(r["time_seconds_new"])
            accepted = r["accepted_new"]
            if not (r["accepted_match"] and r["digest_match"]):
                digest_ok = False

        orig_mean = statistics.mean(orig_times)
        orig_sd = statistics.stdev(orig_times) if len(orig_times) > 1 else 0.0
        new_mean = statistics.mean(new_times)
        new_sd = statistics.stdev(new_times) if len(new_times) > 1 else 0.0
        speedup = orig_mean / new_mean if new_mean > 0 else None

        row = {
            "N": n,
            "accepted": accepted,
            "outcome_identical": digest_ok,
            "orig_mean_s": round(orig_mean, 3),
            "orig_sd_s": round(orig_sd, 3),
            "new_mean_s": round(new_mean, 3),
            "new_sd_s": round(new_sd, 3),
            "speedup_x": round(speedup, 1) if speedup else None,
        }
        rows.append(row)
        flag = "OK" if digest_ok else "MISMATCH!"
        print(f"N={n:>4d}  ref={orig_mean:8.3f}+-{orig_sd:.3f}s  "
              f"incr={new_mean:7.4f}+-{new_sd:.4f}s  speedup={row['speedup_x']}x  [{flag}]")

    with open("results/experiment_scalability_sd.json", "w", encoding="utf-8") as fh:
        json.dump({"repeats": REPEATS, "rows": rows}, fh, indent=2)

    # Ready-to-paste LaTeX rows for tab:scalability
    print("\n" + "=" * 60)
    print("LaTeX rows for Table tab:scalability (paste these):")
    print("=" * 60)
    for r in rows:
        print(f"{r['N']:>3d} & {r['accepted']:>4d} & "
              f"{r['orig_mean_s']:.3f} $\\pm$ {r['orig_sd_s']:.3f} & "
              f"{r['new_mean_s']:.3f} $\\pm$ {r['new_sd_s']:.3f} & "
              f"{r['speedup_x']}$\\times$ \\\\")
        print("\\hline")

    if not all(r["outcome_identical"] for r in rows):
        print("\nWARNING: at least one N produced different outcomes between "
              "resolvers. Do not use these timings until that is fixed.")

    print("\nWrote results/experiment_scalability_sd.json")


if __name__ == "__main__":
    main()