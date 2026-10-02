"""Stable bounded admission for provider session files."""
from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from typing import Literal, Optional, Tuple

from .bounded_json import (
    JSONL_RECORD_LIMIT,
    JSON_NESTING_LIMIT,
    JsonNumberToken,
    parse_jsonl_line,
)
from .source_cohort import FileStamp


PI_IDENTITY_LIMIT = 65_536
INTEGER_DIGIT_LIMIT = 128
Provider = Literal["codex", "pi"]


@dataclass(frozen=True)
class AdmissionProof:
    provider: Provider
    root_identity: Tuple[str, ...]
    raw_digest: bytes


@dataclass(frozen=True)
class AdmittedRecord:
    proof: AdmissionProof
    stamp: FileStamp
    record: dict
    path: str


@dataclass(frozen=True)
class AdmissionResult:
    admitted: Optional[AdmittedRecord]
    stamp: Optional[FileStamp]
    path: str
    failure: str = ""


def _stamp(info: os.stat_result) -> FileStamp:
    return FileStamp(
        info.st_dev,
        info.st_ino,
        stat.S_IMODE(info.st_mode),
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def materialize_json_integer(value: object) -> Optional[int]:
    if type(value) is int:
        return value
    if type(value) is not JsonNumberToken:
        return None
    lexeme = value.lexeme
    digits = lexeme[1:] if lexeme.startswith("-") else lexeme
    if (
        not digits
        or len(digits) > INTEGER_DIGIT_LIMIT
        or any(character not in "0123456789" for character in digits)
    ):
        return None
    return int(lexeme)


def _optional_string(values: dict, name: str) -> bool:
    return name not in values or values[name] is None or type(values[name]) is str


def _codex_identity(value: object) -> Optional[Tuple[str, ...]]:
    if type(value) is not dict or value.get("type") != "session_meta":
        return None
    meta = value.get("payload")
    if type(meta) is not dict or type(meta.get("id")) is not str or not meta["id"]:
        return None
    if not all(
        _optional_string(meta, name)
        for name in (
            "session_id",
            "cwd",
            "thread_source",
            "parent_thread_id",
            "forked_from_id",
            "agent_path",
            "agent_nickname",
        )
    ):
        return None
    source = meta.get("source")
    if source is not None and type(source) not in (str, dict):
        return None
    if type(source) is dict and "subagent" in source:
        subagent = source["subagent"]
        if type(subagent) is not dict:
            return None
        if "thread_spawn" in subagent:
            spawn = subagent["thread_spawn"]
            if spawn is not None:
                if type(spawn) is not dict:
                    return None
                if not all(
                    _optional_string(spawn, name)
                    for name in (
                        "parent_thread_id", "agent_path", "agent_nickname",
                    )
                ):
                    return None
                if "depth" in spawn and spawn["depth"] is not None:
                    depth = materialize_json_integer(spawn["depth"])
                    if depth is None:
                        return None
                    spawn["depth"] = depth
    return (meta.get("session_id") or meta["id"],)


def _pi_identity(value: object) -> Optional[Tuple[str, ...]]:
    if type(value) is not dict or value.get("type") != "session":
        return None
    version = materialize_json_integer(value.get("version"))
    if (
        version is None
        or type(value.get("id")) is not str
        or not value["id"]
        or type(value.get("cwd")) is not str
        or not _optional_string(value, "parentSession")
    ):
        return None
    parent = value.get("parentSession")
    if any(
        len(item) > PI_IDENTITY_LIMIT
        for item in (value["id"], value["cwd"], parent or "")
    ):
        return None
    value["version"] = version
    return (
        "session",
        str(version),
        value["id"],
        value["cwd"],
        parent or "",
    )


def admission_identity(
    value: object,
    provider: Provider,
) -> Optional[Tuple[str, ...]]:
    if provider not in ("codex", "pi"):
        raise ValueError("unknown admission provider")
    return _codex_identity(value) if provider == "codex" else _pi_identity(value)


def admission_from_value(
    value: object,
    provider: Provider,
    raw_digest: bytes = b"",
) -> Optional[Tuple[AdmissionProof, dict]]:
    identity = admission_identity(value, provider)
    if identity is None or type(value) is not dict:
        return None
    return AdmissionProof(provider, identity, raw_digest), value


def _record_failure(raw_line: bytes) -> str:
    if not raw_line:
        return "empty"
    if not raw_line.endswith(b"\n"):
        if b'"timestamp"' in raw_line and b'"session_meta"' in raw_line:
            return "pending"
        return "incomplete"
    raw = raw_line[:-1]
    if raw.endswith(b"\r"):
        raw = raw[:-1]
    if len(raw) > JSONL_RECORD_LIMIT:
        return "oversized"
    return "empty" if not raw.strip() else "invalid"


def read_admission_result(path: str, provider: Provider) -> AdmissionResult:
    if provider not in ("codex", "pi"):
        raise ValueError("unknown admission provider")
    canonical = ""
    try:
        canonical = os.path.realpath(os.path.abspath(os.fspath(path)))
        before_info = os.stat(path)
        if not stat.S_ISREG(before_info.st_mode):
            return AdmissionResult(None, None, canonical, "nonregular")
        before = _stamp(before_info)
        with open(path, "rb") as stream:
            if _stamp(os.fstat(stream.fileno())) != before:
                return AdmissionResult(None, None, canonical, "unstable")
            raw_line = stream.readline(JSONL_RECORD_LIMIT + 2)
            bounded = parse_jsonl_line(raw_line)
            if _stamp(os.fstat(stream.fileno())) != before:
                return AdmissionResult(None, None, canonical, "unstable")
        if (
            _stamp(os.stat(path)) != before
            or os.path.realpath(os.path.abspath(os.fspath(path))) != canonical
        ):
            return AdmissionResult(None, None, canonical, "unstable")
        if bounded is None:
            return AdmissionResult(
                None, before, canonical, _record_failure(raw_line),
            )
        digest = hashlib.blake2b(bounded.raw, digest_size=16).digest()
        admitted = admission_from_value(bounded.value, provider, digest)
        if admitted is None:
            return AdmissionResult(None, before, canonical, "identity")
        proof, record = admitted
        value = AdmittedRecord(proof, before, record, canonical)
        return AdmissionResult(value, before, canonical)
    except (OSError, TypeError, ValueError, OverflowError):
        return AdmissionResult(None, None, canonical, "unavailable")


def read_admission(path: str, provider: Provider) -> Optional[AdmittedRecord]:
    return read_admission_result(path, provider).admitted
