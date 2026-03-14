import json


def load_events(path):
    events = []
    with open(path, "r") as f:
        for line in f:
            events.append(json.loads(line))
    return events


if __name__ == "__main__":
    events = load_events("data/events.jsonl")

    for e in events:
        print(e)