"""Lifecycle facts shared by hooks, session observation, and provider models."""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import re
import stat
import time
from typing import Iterable

from .session_model import LifecycleFact, LifecycleSessionModel


SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
HOOK_EVENTS = {
    "SessionStart",
    "UserPromptSubmit",
    "Stop",
    "SubagentStart",
    "SubagentStop",
    "SessionEnd",
}


def _state_root() -> Path:
    configured = os.environ.get("QUAKEPRO_STATE_DIR")
    root = Path(configured) if configured else (
        Path(os.environ.get("TMPDIR") or "/tmp") / f"quakepro-{os.getuid()}"
    )
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    root_stat = root.lstat()
    if not stat.S_ISDIR(root_stat.st_mode) or root_stat.st_uid != os.getuid():
        raise OSError("unsafe QuakePro state directory")
    if stat.S_IMODE(root_stat.st_mode) != 0o700:
        root.chmod(0o700)
    return root


def lifecycle_path(provider: str, session_id: str) -> Path:
    if provider not in {"claude", "codex"} or not SAFE_ID.fullmatch(session_id):
        raise ValueError("invalid session identity")
    return _state_root() / f"{provider}-{session_id}.events.jsonl"


def fact_from_payload(provider: str, payload: dict) -> LifecycleFact | None:
    session_id = payload.get("session_id")
    event = payload.get("hook_event_name")
    if (
        provider not in {"claude", "codex"}
        or not isinstance(session_id, str)
        or not SAFE_ID.fullmatch(session_id)
        or event not in HOOK_EVENTS
    ):
        return None
    return LifecycleFact(
        provider=provider,
        session_id=session_id,
        event=event,
        agent_id=str(payload.get("agent_id") or "")[:128],
        agent_type=str(payload.get("agent_type") or "")[:128],
        result=str(payload.get("last_assistant_message") or "")[:4000],
        reason=str(payload.get("reason") or "")[:256],
        recorded_ns=time.time_ns(),
    )


def append_fact(fact: LifecycleFact) -> Path:
    path = lifecycle_path(fact.provider, fact.session_id)
    flags = os.O_CREAT | os.O_WRONLY | os.O_APPEND
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        file_stat = os.fstat(fd)
        if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_uid != os.getuid():
            raise OSError("unsafe lifecycle journal")
        if stat.S_IMODE(file_stat.st_mode) != 0o600:
            os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        record = {
            "provider": fact.provider,
            "session_id": fact.session_id,
            "event": fact.event,
            "agent_id": fact.agent_id,
            "agent_type": fact.agent_type,
            "result": fact.result,
            "reason": fact.reason,
            "recorded_ns": fact.recorded_ns,
        }
        os.write(fd, (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    return path


class LifecycleJournal:
    """Tail one secure hook journal."""

    def __init__(self, provider: str, session_id: str) -> None:
        self.provider = provider
        self.session_id = session_id
        self.path = lifecycle_path(provider, session_id)
        self._offset = 0
        self._carry = b""

    def read(self) -> list[LifecycleFact]:
        try:
            size = self.path.stat().st_size
        except OSError:
            return []
        if size < self._offset:
            self._offset = 0
            self._carry = b""
        if size == self._offset:
            return []
        try:
            with self.path.open("rb") as stream:
                stream.seek(self._offset)
                chunk = stream.read(size - self._offset)
        except OSError:
            return []
        self._offset += len(chunk)
        data = self._carry + chunk
        records = data.split(b"\n")
        self._carry = b"" if data.endswith(b"\n") else records.pop()
        facts = []
        for raw in records:
            if not raw.strip():
                continue
            try:
                item = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            fact = self._decode(item)
            if fact:
                facts.append(fact)
        return facts

    def _decode(self, item) -> LifecycleFact | None:
        if not isinstance(item, dict):
            return None
        if item.get("provider") != self.provider or item.get("session_id") != self.session_id:
            return None
        event = item.get("event")
        if event not in HOOK_EVENTS:
            return None
        return LifecycleFact(
            provider=self.provider,
            session_id=self.session_id,
            event=event,
            agent_id=str(item.get("agent_id") or "")[:128],
            agent_type=str(item.get("agent_type") or "")[:128],
            result=str(item.get("result") or "")[:4000],
            reason=str(item.get("reason") or "")[:256],
            recorded_ns=int(item.get("recorded_ns") or 0),
        )


class SessionLifecycle:
    """Apply ordered hook facts and keep authoritative stops terminal."""

    def __init__(self, model: LifecycleSessionModel) -> None:
        self.model = model
        self.ended = False
        self._terminal_agents: dict[str, LifecycleFact] = {}
        self._session_terminal: LifecycleFact | None = None

    def ingest(self, facts: Iterable[LifecycleFact]) -> bool:
        changed = False
        for fact in facts:
            if fact.event in {"SessionStart", "UserPromptSubmit"}:
                self.ended = False
                self._session_terminal = None
                changed |= self.model.apply_lifecycle(fact, "main", "running")
            elif fact.event == "Stop":
                changed |= self.model.apply_lifecycle(fact, "main", "waiting")
            elif fact.event == "SessionEnd":
                self.ended = True
                self._session_terminal = fact
                changed |= self.model.apply_lifecycle(fact, "main", "done")
            elif fact.event == "SubagentStart" and fact.agent_id:
                self._terminal_agents.pop(fact.agent_id, None)
                changed |= self.model.apply_lifecycle(fact, fact.agent_id, "running")
            elif fact.event == "SubagentStop" and fact.agent_id:
                self._terminal_agents[fact.agent_id] = fact
                changed |= self.model.apply_lifecycle(fact, fact.agent_id, "done")
        return changed

    def enforce(self) -> bool:
        changed = False
        if self._session_terminal:
            changed |= self.model.apply_lifecycle(
                self._session_terminal, "main", "done", preserve_error=True
            )
        for agent_id, fact in self._terminal_agents.items():
            changed |= self.model.apply_lifecycle(
                fact, agent_id, "done", preserve_error=True
            )
        return changed
