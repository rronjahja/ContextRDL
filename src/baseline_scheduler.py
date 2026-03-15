from __future__ import annotations

import random
from collections import defaultdict
from typing import Dict, List, Mapping, Optional, Sequence, Tuple


DEFAULT_PREFIX_KEYS = ("roleRank", "priority", "tsKey")


def nondeterministic_schedule(
    actions: Sequence[Mapping[str, object]],
    rng: Optional[random.Random] = None,
) -> List[dict]:
    rng = rng or random.Random()
    shuffled = [dict(action) for action in actions]
    rng.shuffle(shuffled)
    return shuffled


def nondeterministic_tie_schedule(
    actions: Sequence[Mapping[str, object]],
    prefix_keys: Sequence[str] = DEFAULT_PREFIX_KEYS,
    rng: Optional[random.Random] = None,
) -> List[dict]:
    rng = rng or random.Random()
    grouped: Dict[Tuple[object, ...], List[dict]] = defaultdict(list)

    for action in actions:
        prefix = tuple(action[field] for field in prefix_keys)
        grouped[prefix].append(dict(action))

    schedule: List[dict] = []
    for prefix in sorted(grouped):
        group = grouped[prefix]
        rng.shuffle(group)
        schedule.extend(group)

    return schedule


if __name__ == "__main__":
    sample = [
        {"aid": "a1", "roleRank": 1, "priority": 1, "tsKey": 10},
        {"aid": "a2", "roleRank": 1, "priority": 1, "tsKey": 10},
        {"aid": "a3", "roleRank": 0, "priority": 0, "tsKey": 5},
    ]
    print("Full random:", [a["aid"] for a in nondeterministic_schedule(sample)])
    print("Tie random:", [a["aid"] for a in nondeterministic_tie_schedule(sample)])
