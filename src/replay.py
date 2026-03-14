import json
from engine import run_engine


def replay(trace_path="results/trace.json"):

    with open(trace_path, "r") as f:
        trace = json.load(f)

    _, _, _, _, new_trace = run_engine()

    if trace["successor_graph_digest"] == new_trace["successor_graph_digest"]:
        print("Replay successful: successor state reproduced")
    else:
        print("Replay failed: states differ")


if __name__ == "__main__":
    replay()