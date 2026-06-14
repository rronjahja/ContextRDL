from __future__ import annotations

import json
from typing import Any, Dict, List
from pathlib import Path

def load_rules(path: str) -> List[Dict[str, Any]]:
    p = Path(path)
    if not p.is_absolute() and not p.exists():
        p = Path(__file__).resolve().parent.parent / p
    with open(p, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    return data["rules"]


if __name__ == "__main__":
    for rule in load_rules("configs/rules.json"):
        print(rule["rid"], rule["issuing_role"], rule["priority"])