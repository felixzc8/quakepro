"""Provider-neutral session records and interface."""
from __future__ import annotations

import calendar
import time
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


BODY_CLIP = 2000
RESULT_CLIP = 200
DETAIL_CLIP = 70


@dataclass
class Step:
    kind: str
    title: str
    body: str = ""
    ts: float = 0.0
    status: str = "done"
    output: str = ""
    tid: str = ""
    child: str = ""
    bg_path: str = ""
    dispatch: str = ""
    goal: str = ""


@dataclass
class Node:
    id: str
    label: str
    detail: str = ""
    parent: str | None = None
    status: str = "running"
    steps: list[Step] = field(default_factory=list)
    children: list[str] = field(default_factory=list)
    tokens: int = 0
    last_ts: float = 0.0
    model: str = ""
    kind: str = "agent"
    result: str = ""
    skills: list[str] = field(default_factory=list)
    phase: str = ""
    workflow: list[str] = field(default_factory=list)
    finish: str = ""
    active_burst: int = -1


@dataclass
class Task:
    id: str
    subject: str = ""
    status: str = "pending"
    owner: str = ""
    description: str = ""


@dataclass(frozen=True)
class LifecycleFact:
    provider: str
    session_id: str
    event: str
    agent_id: str = ""
    agent_type: str = ""
    result: str = ""
    reason: str = ""
    recorded_ns: int = 0


def parse_timestamp(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        return float(calendar.timegm(time.strptime(value[:19], "%Y-%m-%dT%H:%M:%S")))
    except (TypeError, ValueError, OverflowError):
        return 0.0


def one_line(value: str) -> str:
    for line in (value or "").splitlines():
        line = line.strip()
        if line:
            return line
    return (value or "").strip()


def clip_text(value: str, limit: int) -> str:
    if limit < 1:
        raise ValueError("clip limit must be positive")
    value = one_line(value)
    return value if len(value) <= limit else value[:limit - 1].rstrip() + "…"


@runtime_checkable
class SessionModel(Protocol):
    provider: str
    path: str
    nodes: dict[str, Node]
    task_list: dict[str, list[Task]]

    def poll(self) -> bool: ...

    def observation_paths(self) -> set[str]: ...

    def accepts_observation(self, paths: frozenset[str]) -> bool: ...

    def lifecycle_identity(self) -> tuple[str, str] | None: ...


@runtime_checkable
class LifecycleSessionModel(SessionModel, Protocol):
    def apply_lifecycle(
        self,
        fact: LifecycleFact,
        target: str,
        status: str,
        preserve_error: bool = False,
    ) -> bool: ...
