"""Strict provider input and JSON schema helpers."""
from __future__ import annotations

import math

from .bounded_json import JsonNumberToken


_PATH_FIELDS = frozenset({"file_path", "notebook_path", "path"})
_EDIT_FIELDS = _PATH_FIELDS | frozenset({
    "new_string", "newText", "new_str", "content", "text",
    "old_string", "oldText", "old_str",
})
_PROMPT_FIELDS = frozenset({
    "prompt", "message", "task", "description", "instructions", "task_name",
})


def _recognizer_fields(name: str) -> frozenset[str]:
    if name == "Skill":
        return frozenset({"skill"})
    if name in {"Read", "read", "view"}:
        return _PATH_FIELDS
    if name in {"Bash", "bash", "exec_command"}:
        return frozenset({"command", "cmd"})
    if name == "apply_patch":
        return frozenset({"patch"})
    if name in {"Edit", "Write", "MultiEdit", "NotebookEdit", "edit", "write"}:
        return _EDIT_FIELDS
    if name in {"Task", "Agent", "subagent", "spawn_agent"}:
        return _PROMPT_FIELDS
    return frozenset()


def optional_type(values: dict, name: str, expected: type) -> bool:
    return name not in values or values[name] is None or type(values[name]) is expected


def optional_exact_type(values: dict, name: str, expected: type) -> bool:
    return name not in values or type(values[name]) is expected


def finite_json_value(value: object) -> bool:
    active: set[int] = set()

    def valid(item: object) -> bool:
        if (
            item is None
            or type(item) in (bool, int, str)
            or type(item) is JsonNumberToken
        ):
            return True
        if type(item) is float:
            return math.isfinite(item)
        if type(item) not in (list, dict):
            return False
        marker = id(item)
        if marker in active:
            return False
        active.add(marker)
        try:
            if type(item) is list:
                return all(valid(child) for child in item)
            return all(
                type(key) is str and valid(child)
                for key, child in item.items()
            )
        finally:
            active.remove(marker)

    try:
        return valid(value)
    except RecursionError:
        return False


def validate_recognizer_inputs(name: str, value: object) -> None:
    if type(value) is not dict:
        raise ValueError("invalid recognizer input")
    fields = _recognizer_fields(name)
    if any(
        field in value and type(value[field]) is not str
        for field in fields
    ):
        raise ValueError("invalid recognizer text input")
    if name not in {"Edit", "MultiEdit", "NotebookEdit", "edit"} or "edits" not in value:
        return
    edits = value["edits"]
    if type(edits) is not list:
        raise ValueError("invalid recognizer edits")
    for edit in edits:
        if type(edit) is not dict:
            raise ValueError("invalid recognizer edit")
        if any(
            field in edit and type(edit[field]) is not str
            for field in _EDIT_FIELDS
        ):
            raise ValueError("invalid recognizer edit input")
