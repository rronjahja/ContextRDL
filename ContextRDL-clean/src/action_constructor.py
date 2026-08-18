from __future__ import annotations

from rule_engine import build_action_value, create_action

__all__ = ["build_action_value", "create_action"]


if __name__ == "__main__":
    print("action_constructor delegates to rule_engine.create_action and rule_engine.build_action_value")
