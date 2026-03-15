from __future__ import annotations

import json
from typing import Any, Dict, List


def load_rules(path: str) -> List[Dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    return data["rules"]


if __name__ == "__main__":
    for rule in load_rules("configs/rules.json"):
        print(rule["rid"], rule["issuing_role"], rule["priority"])