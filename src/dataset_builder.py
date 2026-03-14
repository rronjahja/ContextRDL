from rdflib import Dataset, Graph, URIRef, Literal
import json


URN_TIMESTAMP = URIRef("urn:prop:timestamp")
URN_TYPE = URIRef("urn:prop:type")
URN_ROLE = URIRef("urn:prop:role")
URN_ZONE = URIRef("urn:prop:zone")
URN_DELTA = URIRef("urn:prop:delta")
URN_CAP = URIRef("urn:prop:cap")
URN_MODE = URIRef("urn:prop:mode")
URN_TARGET = URIRef("urn:prop:target")
URN_STATE = URIRef("urn:prop:state")


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


def build_dataset(state_graph, events):
    ds = Dataset()

    state = ds.graph(URIRef("urn:state"))
    for triple in state_graph:
        state.add(triple)

    window = ds.graph(URIRef("urn:window"))

    for e in events:
        event_uri = URIRef(f"urn:event:{e['eid']}")
        payload = e.get("payload", {})

        window.add((event_uri, URN_TIMESTAMP, Literal(e["timestamp"])))
        window.add((event_uri, URN_TYPE, Literal(e["type"])))
        window.add((event_uri, URN_ROLE, Literal(e["role"])))

        if "zone" in payload:
            window.add((event_uri, URN_ZONE, URIRef(payload["zone"])))

        if "delta" in payload:
            window.add((event_uri, URN_DELTA, Literal(payload["delta"])))

        if "cap" in payload:
            window.add((event_uri, URN_CAP, Literal(payload["cap"])))

        if "mode" in payload:
            window.add((event_uri, URN_MODE, Literal(payload["mode"])))

        if "target" in payload:
            window.add((event_uri, URN_TARGET, Literal(payload["target"])))

        if "state" in payload:
            window.add((event_uri, URN_STATE, Literal(payload["state"])))

    return ds


if __name__ == "__main__":
    state_graph = load_state("data/base_graph.ttl")
    events = load_events("data/events.jsonl")
    ds = build_dataset(state_graph, events)

    print("Named graphs in dataset:")
    for g in ds.graphs():
        print(g.identifier)