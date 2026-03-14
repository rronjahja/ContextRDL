from engine import run_engine


def run_determinism_experiment(runs=30):

    digests = []

    for i in range(runs):

        _, _, _, _, trace = run_engine()

        digests.append(trace["successor_graph_digest"])

    unique = set(digests)

    print("Runs:", runs)
    print("Unique successor states:", len(unique))

    if len(unique) == 1:
        print("Determinism confirmed")
    else:
        print("Non-deterministic behavior detected")


if __name__ == "__main__":
    run_determinism_experiment()