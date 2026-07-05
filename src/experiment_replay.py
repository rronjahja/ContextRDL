from __future__ import annotations

import json

from engine import run_engine
from replay import replay_trace


def run_replay_experiment(trace_path: str = "results/trace.json"):
    _, _, _, _, trace = run_engine(trace_path=trace_path, save_trace_file=True)
    replay_summary = replay_trace(trace_path=trace_path)

    print("Recorded successor digest:", trace["successor_graph"]["digest"])
    print("Replay summary:")
    print(json.dumps(replay_summary, indent=2, sort_keys=True))

    return replay_summary


if __name__ == "__main__":
    run_replay_experiment()
