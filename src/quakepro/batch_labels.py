"""Visible labels for grouped child-agent launches."""
from __future__ import annotations

import re


_GENERIC = {
    "agent", "agents", "assistant", "batch", "dispatch", "general-purpose",
    "subagent", "sub-agent", "task", "tasks", "unknown", "worker",
}


def explicit_goal(value) -> str:
    """Return one recorded goal when it names work, not a generic role."""
    if not isinstance(value, str):
        return ""
    goal = " ".join(value.split()).strip(" ·")
    if not goal or goal.lower() in _GENERIC:
        return ""
    return goal


def preferred_goal(primary, fallback="") -> str:
    """Choose recorded primary field, then recorded provider fallback."""
    return explicit_goal(primary) or explicit_goal(fallback)


def batch_label(goals, _number: int) -> str:
    """Use one shared recorded goal without guessing a fallback title."""
    usable = list(dict.fromkeys(goal for goal in (explicit_goal(goal) for goal in goals) if goal))
    if not usable:
        return ""
    if len(usable) == 1:
        return usable[0]
    prefix = _shared_prefix(usable)
    if prefix:
        return prefix
    return ""


def _shared_prefix(goals: list[str]) -> str:
    words = [re.split(r"\s+", goal) for goal in goals]
    prefix = []
    for parts in zip(*words):
        if len({part.casefold() for part in parts}) != 1:
            break
        prefix.append(parts[0])
    return " ".join(prefix) if len(prefix) >= 2 else ""
