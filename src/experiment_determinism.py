from __future__ import annotations

from engine import run_engine
from experiment_helpers import tie_conflict_events


def run_determinism_experiment(runs: int = 30):
    successor_digests = []
    execution_digests = []

    for _ in range(runs):
        _, _, _, _, trace = run_engine(events=tie_conflict_events(), save_trace_file=False)
        successor_digests.append(trace["successor_graph"]["digest"])
        execution_digests.append(trace["execution_digest"])

    unique_successors = sorted(set(successor_digests))
    unique_executions = sorted(set(execution_digests))

    print("Runs:", runs)
    print("Unique successor states:", len(unique_successors))
    print("Unique execution traces:", len(unique_executions))
    if len(unique_successors) == 1 and len(unique_executions) == 1:
        print("Determinism confirmed")
    else:
        print("Non-deterministic behavior detected")
        print("Successor digests:")
        for digest in unique_successors:
            print(" -", digest)
        print("Execution digests:")
        for digest in unique_executions:
            print(" -", digest)

    return {
        "runs": runs,
        "unique_successor_states": len(unique_successors),
        "unique_execution_traces": len(unique_executions),
        "successor_digests": unique_successors,
        "execution_digests": unique_executions,
    }


if __name__ == "__main__":
    run_determinism_experiment()