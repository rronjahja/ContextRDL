import json


def load_rules(path):
    with open(path, "r") as f:
        data = json.load(f)
    return data["rules"]


if __name__ == "__main__":

    rules = load_rules("configs/rules.json")

    print("Loaded rules:")

    for r in rules:
        print("RID:", r["rid"])
        print("Priority:", r["priority"])
        print("Role:", r["issuing_role"])
        print()