"""Install and remove QuakePro's Codex auto-open hook."""
from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path
import re
import shlex
import sys
import tempfile
from typing import Any, Optional, Sequence


EVENTS = (
    "SessionStart",
    "UserPromptSubmit",
    "Stop",
    "SubagentStart",
    "SubagentStop",
    "SessionEnd",
)
OWNER = "quakepro.codex-auto-open.v1"
OWNER_FLAG = "--quakepro-hook-id"


class HookConfigError(ValueError):
    """The hook configuration cannot be safely updated."""


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


def _handler(target: Path | str, event: str = "SubagentStart") -> dict[str, Any]:
    command = (
        shlex.quote(str(target.resolve()))
        if isinstance(target, Path)
        else target
    )
    command = f"{command} {OWNER_FLAG} {OWNER}"
    handler = {
        "type": "command",
        "command": command,
        "timeout": 10 if event == "SubagentStart" else 3,
    }
    if event == "SubagentStart":
        handler["statusMessage"] = "Opening QuakePro panel"
    return handler


def _load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        raw = path.read_bytes()
        document = json.loads(raw.decode("utf-8"))
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
        if not isinstance(group, dict):
            raise HookConfigError(f"each hooks.{event} group must be a JSON object")
        handlers = group.get("hooks")
        if not isinstance(handlers, list):
            raise HookConfigError(f"each hooks.{event} group must contain a hooks array")
        if any(not isinstance(handler, dict) for handler in handlers):
            raise HookConfigError(f"each hooks.{event} handler must be a JSON object")
    return groups


def _prunable_group(group: dict[str, Any]) -> bool:
    return group.get("hooks") == [] and set(group) <= {"matcher", "hooks"}


def _install_event(document: dict[str, Any], target: Path | str, event: str) -> None:
    groups = _event_groups(document, event, create=True)
    assert groups is not None
    canonical_group = next((group for group in groups if "matcher" not in group), None)
    if canonical_group is None:
        canonical_group = {"hooks": []}
        groups.append(canonical_group)

    canonical = _handler(target, event)
    installed = False
    emptied: set[int] = set()
    for group in groups:
        handlers = group["hooks"]
        kept = []
        for handler in handlers:
            if not _owned(handler):
                kept.append(handler)
            elif group is canonical_group and not installed:
                kept.append(canonical)
                installed = True
        if len(kept) != len(handlers):
            emptied.add(id(group))
        group["hooks"] = kept

    if not installed:
        canonical_group["hooks"].append(canonical)
    groups[:] = [
        group for group in groups
        if not (id(group) in emptied and group is not canonical_group and _prunable_group(group))
    ]


def _merge_install(document: dict[str, Any], target: Path | str) -> None:
    for event in EVENTS:
        _install_event(document, target, event)


def _uninstall_event(document: dict[str, Any], event: str) -> None:
    groups = _event_groups(document, event, create=False)
    if groups is None:
        return
    removed_groups: set[int] = set()
    for group in groups:
        handlers = group["hooks"]
        kept = [handler for handler in handlers if not _owned(handler)]
        if len(kept) != len(handlers):
            removed_groups.add(id(group))
            group["hooks"] = kept
    groups[:] = [
        group for group in groups
        if not (id(group) in removed_groups and _prunable_group(group))
    ]

    hooks = document["hooks"]
    if not groups:
        del hooks[event]
    if not hooks:
        del document["hooks"]


def _merge_uninstall(document: dict[str, Any]) -> None:
    for event in EVENTS:
        _uninstall_event(document, event)


def _write_atomic(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    data = (json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
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


def _update(path: Path, operation: str, target: Path | str) -> bool:
    document = _load(path)
    original = copy.deepcopy(document)
    if operation == "install":
        _merge_install(document, target)
    else:
        _merge_uninstall(document)
    if document == original:
        return False
    _write_atomic(path, document)
    return True


def has_inline_hooks(config_path: Path) -> bool:
    try:
        text = config_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return False
    except OSError:
        return False
    return bool(re.search(
        r"(?m)^[ \t]*(?:\[\[[ \t]*hooks[ \t]*\.|\[[ \t]*hooks[ \t]*\]|hooks[ \t]*=)",
        text,
    ))


def _validate_runtime(script_path: Path) -> None:
    root = script_path.parent
    package = root / "src" / "quakepro"
    required = (
        script_path,
        package / "__init__.py",
        package / "app.py",
        package / "pane_lifecycle.py",
        package / "lifecycle.py",
        package / "session_model.py",
        package / "bounded_json.py",
        package / "provider_admission.py",
        package / "provider_schema.py",
        package / "session_identity.py",
        package / "source_cohort.py",
        root / ".venv" / "bin" / "python",
    )
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise HookConfigError("QuakePro runtime is incomplete: " + ", ".join(missing))
    if not os.access(script_path, os.X_OK) or not os.access(required[-1], os.X_OK):
        raise HookConfigError("codex-open-pane.sh and .venv/bin/python must be executable")


def _user_codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME")
    if configured is None or not configured.strip():
        return (Path.home() / ".codex").resolve()
    return Path(configured).expanduser().resolve()


def _project_root(explicit_root: Optional[str]) -> Path:
    start = Path(explicit_root).expanduser() if explicit_root else Path.cwd()
    try:
        start = start.resolve(strict=True)
    except OSError as exc:
        raise HookConfigError(f"project root does not exist: {start}") from exc
    if not start.is_dir():
        raise HookConfigError(f"project root is not a directory: {start}")
    if explicit_root:
        if not (start / ".git").exists():
            raise HookConfigError(f"explicit project root is not a Git repository: {start}")
        return start
    for candidate in (start, *start.parents):
        if (candidate / ".git").exists():
            return candidate
    raise HookConfigError(f"cannot find a Git project from: {start}")


def _paths(scope: str, explicit_root: Optional[str] = None) -> tuple[Path, Path]:
    user_home = _user_codex_home()
    if scope == "user":
        if explicit_root:
            raise HookConfigError("--root requires --scope project")
        return user_home / "hooks.json", user_home / "config.toml"
    project_root = _project_root(explicit_root)
    project_home = project_root / ".codex"
    project_hooks = project_home / "hooks.json"
    user_hooks = user_home / "hooks.json"
    if project_hooks.resolve() == user_hooks.resolve():
        raise HookConfigError("project hook target aliases the user hook configuration")
    return project_hooks, project_home / "config.toml"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="quakepro hooks")
    parser.add_argument("operation", choices=("install", "uninstall"))
    parser.add_argument("--scope", choices=("user", "project"), default="user")
    parser.add_argument("--root", help="Git repository root for project scope")
    return parser


def configure(
    operation: str,
    target: Path | str,
    scope: str = "user",
    root: Optional[str] = None,
) -> tuple[bool, Path, Path]:
    path, config_path = _paths(scope, root)
    if operation == "install" and isinstance(target, Path):
        _validate_runtime(target)
    return _update(path, operation, target), path, config_path


def diagnose(
    command: str,
    scope: str = "user",
    root: Optional[str] = None,
) -> tuple[Path, tuple[str, ...]]:
    path, _ = _paths(scope, root)
    document = _load(path)
    problems = []
    for event in EVENTS:
        groups = _event_groups(document, event, create=False) or []
        owned = [
            handler
            for group in groups
            for handler in group["hooks"]
            if _owned(handler)
        ]
        if owned != [_handler(command, event)]:
            problems.append(event)
    return path, tuple(problems)


def main(argv: Sequence[str] | None = None, hook_command: str | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        script_path = Path(__file__).resolve().parents[2] / "codex-open-pane.sh"
        target: Path | str = hook_command or script_path
        changed, path, config_path = configure(
            args.operation, target, args.scope, args.root,
        )
    except (HookConfigError, OSError) as exc:
        print(f"quakepro hooks: {exc}", file=sys.stderr)
        return 1

    state = "Installed" if args.operation == "install" else "Removed"
    if not changed:
        state = "Already installed" if args.operation == "install" else "Already absent"
    print(f"{state}: {path}")
    if has_inline_hooks(config_path):
        print(
            f"Warning: {config_path} also defines inline hooks; Codex loads both sources and warns.",
            file=sys.stderr,
        )
    if args.operation == "install":
        print("Restart Codex, then open /hooks to review and trust the QuakePro hook.")
        if args.scope == "project":
            print("Project hooks load only after Codex trusts this repository.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
