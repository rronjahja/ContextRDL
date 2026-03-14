import hashlib
import json


def graph_digest(graph):

    triples = sorted([str(t) for t in graph])
    data = "\n".join(triples)

    return hashlib.sha256(data.encode()).hexdigest()


def build_trace(G_t, Act_t, Sigma_t, B_t, G_next):

    trace = {
        "input_graph_digest": graph_digest(G_t),
        "enabled_actions": [a["aid"] for a in Act_t],
        "schedule": [a["aid"] for a in Sigma_t],
        "accepted_actions": [a["aid"] for a in B_t],
        "successor_graph_digest": graph_digest(G_next)
    }

    return trace


def save_trace(trace, path="results/trace.json"):

    with open(path, "w") as f:
        json.dump(trace, f, indent=2)