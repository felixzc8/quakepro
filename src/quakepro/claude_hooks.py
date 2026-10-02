"""Install and remove QuakePro hooks in Claude Code settings."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import shlex
import tempfile
from typing import Any, Optional


EVENTS = (
    "SessionStart",
    "UserPromptSubmit",
    "Stop",
    "SubagentStart",
    "SubagentStop",
    "SessionEnd",
)
OWNER = "quakepro.claude-auto-open.v1"
OWNER_FLAG = "--quakepro-hook-id"


class HookConfigError(ValueError):
    pass


def _owned(handler: Any) -> bool:
    if not isinstance(handler, dict) or not isinstance(handler.get("command"), str):
        return False
    try:
        words = shlex.split(handler["command"])
    except ValueError:
        return False
    return any(
        words[index] == OWNER_FLAG and words[index + 1] == OWNER
        for index in range(len(words) - 1)
    )


def _handler(command: str, event: str) -> dict[str, Any]:
    handler: dict[str, Any] = {
        "type": "command",
        "command": f"{command} {OWNER_FLAG} {OWNER}",
        "timeout": 10 if event in {"SubagentStart", "SessionEnd"} else 3,
    }
    if event == "SubagentStart":
        handler["async"] = True
        handler["statusMessage"] = "Opening QuakePro panel"
    return handler


def _load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HookConfigError(f"cannot parse hook configuration: {path}") from exc
    if not isinstance(document, dict):
        raise HookConfigError(f"hook configuration must contain a JSON object: {path}")
    return document


def _event_groups(document: dict[str, Any], event: str, create: bool) -> list[Any] | None:
    if "hooks" not in document:
        if not create:
            return None
        hooks = {}
        document["hooks"] = hooks
    else:
        hooks = document["hooks"]
    if not isinstance(hooks, dict):
        raise HookConfigError("the top-level 'hooks' value must be a JSON object")
    if event not in hooks:
        if not create:
            return None
        groups = []
        hooks[event] = groups
    else:
        groups = hooks[event]
    if not isinstance(groups, list):
        raise HookConfigError(f"hooks.{event} must be a JSON array")
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
            raise HookConfigError(f"each hooks.{event} group must contain a hooks array")
        if any(not isinstance(handler, dict) for handler in group["hooks"]):
            raise HookConfigError(f"each hooks.{event} handler must be a JSON object")
    return groups


def _prunable(group: dict[str, Any]) -> bool:
    return group.get("hooks") == [] and set(group) <= {"matcher", "hooks"}


def _install_event(document: dict[str, Any], command: str, event: str) -> None:
    groups = _event_groups(document, event, True)
    assert groups is not None
    canonical = next((group for group in groups if "matcher" not in group), None)
    if canonical is None:
        canonical = {"hooks": []}
        groups.append(canonical)
    installed = False
    emptied: set[int] = set()
    replacement = _handler(command, event)
    for group in groups:
        kept = []
        for handler in group["hooks"]:
            if not _owned(handler):
                kept.append(handler)
            elif group is canonical and not installed:
                kept.append(replacement)
                installed = True
        if len(kept) != len(group["hooks"]):
            emptied.add(id(group))
        group["hooks"] = kept
    if not installed:
        canonical["hooks"].append(replacement)
    groups[:] = [
        group for group in groups
        if not (id(group) in emptied and group is not canonical and _prunable(group))
    ]


def _uninstall_event(document: dict[str, Any], event: str) -> None:
    groups = _event_groups(document, event, False)
    if groups is None:
        return
    removed: set[int] = set()
    for group in groups:
        kept = [handler for handler in group["hooks"] if not _owned(handler)]
        if len(kept) != len(group["hooks"]):
            removed.add(id(group))
            group["hooks"] = kept
    groups[:] = [
        group for group in groups if not (id(group) in removed and _prunable(group))
    ]
    if not groups:
        del document["hooks"][event]
    if document.get("hooks") == {}:
        del document["hooks"]


def _write_atomic(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    data = (json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode()
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    temporary_path = Path(temporary)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        temporary_path.unlink(missing_ok=True)
        raise


def _update(path: Path, operation: str, command: str) -> bool:
    document = _load(path)
    original = copy.deepcopy(document)
    if operation == "install":
        for event in EVENTS:
            _install_event(document, command, event)
    else:
        for event in EVENTS:
            _uninstall_event(document, event)
    if document == original:
        return False
    _write_atomic(path, document)
    return True


def _project_root(explicit_root: Optional[str]) -> Path:
    start = Path(explicit_root).expanduser() if explicit_root else Path.cwd()
    try:
        start = start.resolve(strict=True)
    except OSError as exc:
        raise HookConfigError(f"project root does not exist: {start}") from exc
    if not start.is_dir():
        raise HookConfigError(f"project root is not a directory: {start}")
    for candidate in (start, *start.parents):
        if (candidate / ".git").exists():
            return candidate
    raise HookConfigError(f"cannot find a Git project from: {start}")


def _paths(scope: str, explicit_root: Optional[str] = None) -> Path:
    user_home = Path(
        os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude"
    ).expanduser().resolve()
    if scope == "user":
        if explicit_root:
            raise HookConfigError("--root requires --scope project")
        return user_home / "settings.json"
    project = _project_root(explicit_root)
    target = project / ".claude" / "settings.json"
    if target.resolve() == (user_home / "settings.json").resolve():
        raise HookConfigError("project hook target aliases the user hook configuration")
    return target


def configure(
    operation: str,
    command: str,
    scope: str = "user",
    root: Optional[str] = None,
) -> tuple[bool, Path]:
    path = _paths(scope, root)
    return _update(path, operation, command), path


def diagnose(
    command: str,
    scope: str = "user",
    root: Optional[str] = None,
) -> tuple[Path, tuple[str, ...]]:
    path = _paths(scope, root)
    document = _load(path)
    problems = []
    for event in EVENTS:
        groups = _event_groups(document, event, False) or []
        owned = [
            handler
            for group in groups
            for handler in group["hooks"]
            if _owned(handler)
        ]
        if owned != [_handler(command, event)]:
            problems.append(event)
    return path, tuple(problems)
