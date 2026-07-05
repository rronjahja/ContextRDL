from dataset_builder import load_events


if __name__ == "__main__":
    for event in load_events("data/events.jsonl"):
        print(event)