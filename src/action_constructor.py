import hashlib


def create_action(rule, binding, window_id="w1"):

    rid = rule["rid"]

    zone = str(binding[0])
    setpoint = str(binding[1])

    bind_key = f"{zone}-{setpoint}"

    aid_source = f"{rid}|{bind_key}|{window_id}"
    aid = hashlib.sha256(aid_source.encode()).hexdigest()

    action = {
        "aid": aid,
        "rid": rid,
        "zone": zone,
        "currentSetpoint": setpoint,
        "priority": rule["priority"]
    }

    return action


if __name__ == "__main__":

    dummy_rule = {
        "rid": "r1",
        "priority": 1
    }

    dummy_binding = (
        "http://example.org/building#ZoneA",
        "22"
    )

    action = create_action(dummy_rule, dummy_binding)

    print(action)