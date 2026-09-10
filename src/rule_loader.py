from __future__ import annotations

import json
from typing import Any, Dict, List
from pathlib import Path

def validate_rules(rules: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    for rule in rules:
        rid = rule.get("rid")
        if not isinstance(rid, str) or not rid:
            raise ValueError("Every rule must have a non-empty string identifier")
        if rid in seen:
            raise ValueError(f"Duplicate rule identifier: {rid}")
        seen.add(rid)
    return rules

def load_rules(path: str) -> List[Dict[str, Any]]:
    p = Path(path)
    if not p.is_absolute() and not p.exists():
        p = Path(__file__).resolve().parent.parent / p
    with open(p, "r", encoding="utf-8") as handle:
        data = json.load(handle, parse_float=str)
    return validate_rules(data["rules"])


if __name__ == "__main__":
    for rule in load_rules("configs/rules.json"):
        print(rule["rid"], rule["issuing_role"], rule["priority"])
