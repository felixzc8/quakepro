"""Provider detection and Codex session identity."""
from __future__ import annotations

from dataclasses import dataclass
import os
import re
from typing import Any, Iterator, Optional

from .bounded_json import (
    JSONL_RECORD_LIMIT,
    decode_json_record,
    validate_json_record,
)
from .provider_admission import read_admission
from .provider_schema import finite_json_value, optional_type


def claude_projects() -> str:
    config = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    return os.path.join(config, "projects")


def codex_sessions() -> str:
    home = os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
    return os.path.join(home, "sessions")


def claude_project_dir(root: str) -> str:
    """The transcript directory Claude Code uses for a working directory: the
    absolute path with every non-alphanumeric character replaced by a dash."""
    return os.path.join(claude_projects(),
                        re.sub(r"[^A-Za-z0-9]", "-", os.path.abspath(os.path.expanduser(root))))


def records(path: str, limit: int = 64) -> Iterator[dict]:
    try:
        with open(path, "rb") as stream:
            for _ in range(limit):
                line = stream.readline(JSONL_RECORD_LIMIT + 2)
                if not line:
                    break
                raw = line[:-1] if line.endswith(b"\n") else line
                if len(raw) > JSONL_RECORD_LIMIT:
                    raise ValueError(f"session record exceeds 64 MiB: {path}")
                try:
                    if not validate_json_record(raw):
                        continue
                    record = decode_json_record(raw)
                except ValueError:
                    continue
                if isinstance(record, dict):
                    yield record
    except UnicodeDecodeError as exc:
        raise ValueError(f"cannot decode session transcript: {path}") from exc
    except OSError as exc:
        raise ValueError(f"cannot read session transcript: {path}") from exc


def _valid_codex_meta(value: object) -> bool:
    if (
        type(value) is not dict
        or not finite_json_value(value)
        or type(value.get("id")) is not str
        or not value["id"]
    ):
        return False
    if not all(
        optional_type(value, field, str)
        for field in (
            "session_id", "cwd", "thread_source", "parent_thread_id",
            "agent_path", "agent_nickname",
        )
    ):
        return False
    source = value.get("source")
    if source is not None and type(source) not in (str, dict):
        return False
    if type(source) is not dict or "subagent" not in source:
        return True
    subagent = source["subagent"]
    if type(subagent) is not dict:
        return False
    spawn = subagent.get("thread_spawn")
    if spawn is None:
        return True
    return type(spawn) is dict and all(
        optional_type(spawn, field, int if field == "depth" else str)
        for field in (
            "parent_thread_id", "depth", "agent_path", "agent_nickname",
        )
    )


def codex_meta(path: str) -> Optional[dict[str, Any]]:
    admitted = read_admission(path, "codex")
    if admitted is None:
        return None
    record = admitted.record
    if record is None or record.get("type") != "session_meta":
        return None
    payload = record.get("payload")
    if not _valid_codex_meta(payload):
        return None
    group = payload.get("session_id") or payload["id"]
    return payload if admitted.proof.root_identity == (group,) else None


def first_codex_meta(path: str) -> dict:
    try:
        return codex_meta(path) or {}
    except ValueError:
        return {}


def detect_provider(path: str) -> str:
    if read_admission(path, "codex") is not None:
        return "codex"
    if read_admission(path, "pi") is not None:
        return "pi"
    saw_record = False
    for record in records(path):
        saw_record = True
        if record.get("type") in {"assistant", "user", "progress"} and (
            "message" in record or "data" in record
        ):
            return "claude"
        if record.get("type") in {"queue-operation", "file-history-snapshot"} and record.get(
            "sessionId"
        ):
            return "claude"
    problem = "unknown" if saw_record else "empty or malformed"
    raise ValueError(f"{problem} session transcript: {path}")


def is_codex_child(meta: dict[str, Any]) -> bool:
    if meta.get("thread_source") == "subagent":
        return True
    source = meta.get("source")
    if isinstance(source, dict) and isinstance(source.get("subagent"), dict):
        return True
    thread_id = str(meta.get("id") or "")
    session_id = str(meta.get("session_id") or thread_id)
    return bool(meta.get("parent_thread_id") and session_id != thread_id)


def internal_codex_subagent(meta: dict) -> bool:
    source = meta.get("source")
    if not isinstance(source, dict):
        return False
    subagent = source.get("subagent")
    return isinstance(subagent, dict) and "other" in subagent and "thread_spawn" not in subagent


def sessions_root(path: str) -> str:
    current = os.path.abspath(os.path.dirname(path))
    while os.path.dirname(current) != current:
        if os.path.basename(current) == "sessions":
            return current
        current = os.path.dirname(current)
    return os.path.abspath(os.path.dirname(path))


def index_codex_sessions(root: str, requested_path: str = "") -> dict[str, dict]:
    found = {}
    for directory, _, names in os.walk(root):
        for name in names:
            if not name.endswith(".jsonl"):
                continue
            path = os.path.join(directory, name)
            meta = first_codex_meta(path)
            if meta.get("id"):
                found[path] = meta
    if requested_path and requested_path not in found:
        meta = first_codex_meta(requested_path)
        if meta:
            found[requested_path] = meta
    return found


@dataclass(frozen=True)
class CodexSessionIdentity:
    requested_path: str
    sessions_root: str
    requested_meta: dict
    metadata: dict[str, dict]
    root_path: str
    root_meta: dict
    group_id: str
    root_thread: str
    root_resolved: bool


def resolve_codex_session(path: str) -> CodexSessionIdentity:
    requested = os.path.abspath(path)
    root = sessions_root(requested)
    requested_meta = first_codex_meta(requested)
    if not requested_meta.get("id"):
        raise ValueError("not a Codex rollout")
    group_id = str(requested_meta.get("session_id") or requested_meta["id"])
    requested_id = str(requested_meta["id"])
    if group_id == requested_id and not is_codex_child(requested_meta):
        metadata = {requested: requested_meta}
    else:
        metadata = index_codex_sessions(os.path.dirname(requested), requested)
        if not any(
            str(meta.get("id") or "") == group_id for meta in metadata.values()
        ):
            metadata = index_codex_sessions(root, requested)
    root_path = next(
        (
            candidate
            for candidate, meta in metadata.items()
            if str(meta.get("id") or "") == group_id
            and str(meta.get("session_id") or meta.get("id") or "") == group_id
            and not is_codex_child(meta)
        ),
        "",
    )
    root_resolved = bool(root_path)
    if not root_path and internal_codex_subagent(requested_meta):
        raise ValueError("internal Codex subagent rollout is not monitorable")
    root_path = root_path or requested
    root_meta = metadata.get(root_path) or first_codex_meta(root_path)
    resolved_group = str(root_meta.get("session_id") or group_id)
    root_thread = str(root_meta.get("id") or group_id)
    return CodexSessionIdentity(
        requested,
        root,
        requested_meta,
        metadata,
        root_path,
        root_meta,
        resolved_group,
        root_thread,
        root_resolved,
    )


def hook_transcript_matches(path: str, session_id: str) -> bool:
    meta = first_codex_meta(path)
    thread_id = str(meta.get("id") or "")
    return bool(thread_id and str(meta.get("session_id") or thread_id) == session_id)
