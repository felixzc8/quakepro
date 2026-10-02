"""Install QuakePro's pi auto-open extension."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
from typing import Optional


OWNER = "quakepro.pi-auto-open.v1"
MARKER = f"// {OWNER}"


class HookConfigError(ValueError):
    pass


def _extension(executable: str) -> str:
    command = json.dumps(executable, ensure_ascii=False)
    owner = json.dumps(OWNER)
    return f'''{MARKER}
import {{ spawn }} from "node:child_process";
import {{ closeSync, openSync, readSync }} from "node:fs";

const executable = {command};
const owner = {owner};
let openedSessionId;
let retryTimer;
let activeContext;

function readyTranscript(path, sessionId) {{
  let descriptor;
  try {{
    descriptor = openSync(path, "r");
    const buffer = Buffer.alloc(65536);
    const length = readSync(descriptor, buffer, 0, buffer.length, 0);
    const newline = buffer.subarray(0, length).indexOf(10);
    if (newline < 0) return false;
    const header = JSON.parse(buffer.subarray(0, newline).toString("utf8"));
    return header.type === "session" && header.id === sessionId;
  }} catch {{
    return false;
  }} finally {{
    if (descriptor !== undefined) closeSync(descriptor);
  }}
}}

function session(ctx) {{
  return {{
    cwd: ctx.cwd,
    sessionId: ctx.sessionManager.getSessionId(),
    transcriptPath: ctx.sessionManager.getSessionFile(),
  }};
}}

function emit(hookEventName, current) {{
  const payload = JSON.stringify({{
    hook_event_name: hookEventName,
    session_id: current.sessionId,
    transcript_path: current.transcriptPath,
    cwd: current.cwd,
  }});
  try {{
    const child = spawn(
      executable,
      ["_hook", "pi", "--quakepro-hook-id", owner],
      {{ cwd: current.cwd, detached: true, stdio: ["pipe", "ignore", "ignore"] }},
    );
    child.on("error", () => {{}});
    child.stdin.on("error", () => {{}});
    child.stdin.end(payload);
    child.unref();
    return true;
  }} catch {{
    return false;
  }}
}}

function tryOpen(current) {{
  if (openedSessionId === current.sessionId) return true;
  if (
    !current.transcriptPath ||
    !readyTranscript(current.transcriptPath, current.sessionId)
  ) return false;
  if (!emit("SessionStart", current)) return false;
  openedSessionId = current.sessionId;
  return true;
}}

function open(ctx) {{
  if (!ctx.hasUI) return;
  const current = session(ctx);
  if (tryOpen(current) || !current.transcriptPath || retryTimer !== undefined) return;
  let attempts = 0;
  const retry = () => {{
    retryTimer = undefined;
    if (tryOpen(current) || attempts++ >= 100) return;
    retryTimer = setTimeout(retry, 50);
  }};
  retryTimer = setTimeout(retry, 50);
}}

function remember(ctx) {{
  activeContext = ctx;
}}

function foregroundLaunch(event) {{
  return (
    event.toolName === "subagent" &&
    event.input !== null &&
    typeof event.input === "object" &&
    event.input.action === undefined &&
    event.input.async !== true
  );
}}

function sameSession(payload, current) {{
  return (
    !payload.sessionId ||
    payload.sessionId === current.sessionId ||
    payload.sessionId === current.transcriptPath
  );
}}

function asyncStart(payload) {{
  if (!activeContext || payload === null || typeof payload !== "object") return;
  try {{
    const current = session(activeContext);
    if (!sameSession(payload, current)) return;
    open(activeContext);
  }} catch {{}}
}}

function close(ctx) {{
  if (retryTimer !== undefined) clearTimeout(retryTimer);
  retryTimer = undefined;
  const current = session(ctx);
  if (openedSessionId !== current.sessionId) {{
    activeContext = undefined;
    return;
  }}
  emit("SessionEnd", current);
  openedSessionId = undefined;
  activeContext = undefined;
}}

export default function (pi) {{
  pi.on("session_start", (_event, ctx) => remember(ctx));
  pi.on("session_switch", (_event, ctx) => remember(ctx));
  pi.on("tool_call", (event, ctx) => {{
    if (foregroundLaunch(event)) open(ctx);
  }});
  pi.events.on("subagent:async-started", (payload) => asyncStart(payload));
  pi.on("session_shutdown", (_event, ctx) => close(ctx));
}}
'''


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


def _path(scope: str, explicit_root: Optional[str] = None) -> Path:
    configured = os.environ.get("PI_CODING_AGENT_DIR")
    user_home = Path(configured).expanduser() if configured else Path.home() / ".pi" / "agent"
    user_target = (user_home / "extensions" / "quakepro.js").resolve()
    if scope == "user":
        if explicit_root:
            raise HookConfigError("--root requires --scope project")
        return user_target
    target = _project_root(explicit_root) / ".pi" / "extensions" / "quakepro.js"
    if target.resolve() == user_target:
        raise HookConfigError("project hook target aliases user extension")
    return target


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        raise HookConfigError(f"cannot read pi extension: {path}") from exc


def _owned(text: str | None) -> bool:
    return bool(text and text.startswith(MARKER + "\n"))


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    temporary_path = Path(temporary)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
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


def configure(
    operation: str,
    executable: str,
    scope: str = "user",
    root: Optional[str] = None,
) -> tuple[bool, Path]:
    path = _path(scope, root)
    current = _read(path)
    if operation == "install":
        expected = _extension(executable)
        if current == expected:
            return False, path
        if current is not None and not _owned(current):
            raise HookConfigError(f"refusing to replace unrelated pi extension: {path}")
        _write_atomic(path, expected)
        return True, path
    if operation != "uninstall":
        raise HookConfigError(f"unknown operation: {operation}")
    if current is None:
        return False, path
    if not _owned(current):
        raise HookConfigError(f"refusing to remove unrelated pi extension: {path}")
    path.unlink()
    return True, path


def diagnose(
    executable: str,
    scope: str = "user",
    root: Optional[str] = None,
) -> tuple[Path, tuple[str, ...]]:
    path = _path(scope, root)
    current = _read(path)
    if current is None:
        return path, ("missing",)
    if not _owned(current):
        return path, ("unrelated file",)
    if current != _extension(executable):
        return path, ("stale extension",)
    return path, ()
