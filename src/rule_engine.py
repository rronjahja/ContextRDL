from rdflib import Graph
import json
import hashlib


def load_state(path):
    g = Graph()
    g.parse(path, format="turtle")
    return g


def load_events(path):
    events = []
    with open(path, "r") as f:
        for line in f:
            events.append(json.loads(line))
    return events


def load_rules(path):
    with open(path, "r") as f:
        data = json.load(f)
    return data["rules"]


def load_settings():
    with open("configs/settings.json") as f:
        return json.load(f)


def build_dataset(state_graph, events):
    from dataset_builder import build_dataset as _build_dataset
    return _build_dataset(state_graph, events)


def create_action(rule, binding, window_id="w1"):
    settings = load_settings()

    rid = rule["rid"]
    role = rule["issuing_role"]
    role_rank = settings["role_precedence"][role]

    zone = str(binding[0])
    observed_value = str(binding[1])

    bind_key = f"{zone}-{observed_value}"
    aid_source = f"{rid}|{bind_key}|{window_id}"
    aid = hashlib.sha256(aid_source.encode()).hexdigest()

    predicate = rule["insert_template"]["predicate"]

    if rid == "r1":
        new_value = str(int(float(observed_value)) + 2)

    elif rid == "r2":
        new_value = "21"

    elif rid == "r3":
        new_value = "high"

    elif rid == "r4":
        new_value = str(int(float(observed_value)) + 1)

    elif rid == "r5":
        new_value = "true"

    elif rid == "r6":
        new_value = "emergency"

    elif rid == "r7":
        new_value = "22"

    elif rid == "r8":
        new_value = "off"

    else:
        new_value = observed_value

    action = {
        "aid": aid,
        "rid": rid,
        "zone": zone,
        "predicate": predicate,
        "value": new_value,
        "priority": rule["priority"],
        "role": role,
        "roleRank": role_rank,
        "tsKey": 0,
        "bindKey": bind_key
    }

    return action


def evaluate_rules(dataset, rules):
    Act_t = []

    for rule in rules:
        results = dataset.query(rule["condition_select"])

        for row in results:
            action = create_action(rule, row)
            Act_t.append(action)

    return Act_t


def schedule_actions(actions):
    return sorted(
        actions,
        key=lambda a: (
            a["roleRank"],
            a["priority"],
            a["tsKey"],
            a["rid"],
            a["bindKey"],
            a["aid"],
        ),
    )


if __name__ == "__main__":
    from dataset_builder import load_events, load_state, build_dataset

    state = load_state("data/base_graph.ttl")
    events = load_events("data/events.jsonl")
    rules = load_rules("configs/rules.json")

    ds = build_dataset(state, events)

    Act_t = evaluate_rules(ds, rules)

    print("\nAct_t (unsorted):\n")
    for a in Act_t:
        print(a)

    Sigma_t = schedule_actions(Act_t)

    print("\nDeterministic schedule Σ_t:\n")
    for a in Sigma_t:
        print(a["rid"], a["aid"])