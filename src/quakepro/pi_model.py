"""pi session adapter for QuakePro's provider-neutral node model.

Schema source of truth is pi's own shipped documentation and code in
`@mariozechner/pi-coding-agent` (verified against v0.73.1). Core records were
also replayed from a live `@earendil-works/pi-coding-agent` v0.83.0 session:

  docs/session-format.md            entry types, message roles, content blocks
  dist/core/session-manager.js:212  session directory encoding
  dist/config.js:339-341            PI_CODING_AGENT_DIR, PI_CODING_AGENT_SESSION_DIR
  dist/core/tools/{bash,edit,write}.js   tool argument names
  pi-subagents/docs/observability.md async run artifacts and status projection

A session is one append-only JSONL file. Its first line is a `session` header
(`cwd`, `version`, session uuid); every later line is a tree entry carrying `id`
and `parentId`. QuakePro renders the entries in file order — the order pi wrote
them — so a `/tree` branch appears as later actions rather than a separate view.

pi core ships no sub-agent tool. Current pi-subagents releases persist live async
status and child sessions. Older foreground releases embedded child transcripts
in `details.results[]`; QuakePro supports both forms.
"""
from __future__ import annotations

import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass

from . import skill_mode
from . import workflow
from .batch_labels import batch_label, preferred_goal
from .batch_model import sync_batch_statuses
from .provider_admission import (
    admission_from_value,
    read_admission,
)
from .provider_reload import (
    CapturedCohort,
    CapturedFile,
    ReloadPlan,
    poll_append,
    strict_json_record,
    source_revision as _source_revision,
    visible_revision as _visible_revision,
)
from .provider_schema import (
    optional_type as _optional_type,
    validate_recognizer_inputs,
)
from .session_model import (
    BODY_CLIP,
    DETAIL_CLIP,
    RESULT_CLIP,
    Node,
    Step,
    clip_text,
    one_line,
    parse_timestamp,
)
PROVIDER = "pi"
SPAWN_TOOL = "subagent"
# Detail line for a tool call: the first of pi's own argument names that is set.
_DETAIL_KEYS = ("command", "path", "pattern", "query", "url", "task", "agent")
_ACTIVE_STOP = {"toolUse"}
_FAILED_STOP = {"error", "aborted"}
_ASYNC_STATUS_LIMIT = 8 * 1024 * 1024
_ASYNC_TERMINAL = {"complete", "completed", "failed", "paused", "rejected", "stopped"}
_ASYNC_ERROR = {"failed", "rejected", "stopped"}
_ASYNC_CHILD_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


@dataclass(frozen=True)
class _PreparedPiAppend:
    action: str
    source: CapturedFile | None
    records: tuple[dict, ...]
    child_records: tuple[tuple[str, tuple[dict, ...]], ...] = ()
    header: dict | None = None
    header_digest: bytes = b""


def _pi_followup(candidate) -> ReloadPlan:
    return candidate.reload_plan()


def _valid_pi_header(value: object) -> bool:
    return (
        admission_from_value(value, "pi") is not None
        and type(value) is dict
        and _optional_type(value, "timestamp", str)
    )


def _pi_header_identity(value: object):
    if not _valid_pi_header(value):
        return None
    return (
        value["type"],
        value["version"],
        value["id"],
        value["cwd"],
        value.get("parentSession"),
    )


def _async_child_identity(parent_path: str, value: object) -> tuple[str, str] | None:
    if type(value) is not str or not os.path.isabs(value):
        return None
    path = os.path.realpath(value)
    if os.path.normpath(value) != value or os.path.basename(path) != "session.jsonl":
        return None
    parent = os.path.realpath(parent_path)
    stem = os.path.splitext(os.path.basename(parent))[0]
    root = os.path.join(os.path.dirname(parent), stem)
    try:
        relative = os.path.relpath(path, root)
        if os.path.commonpath((path, root)) != root:
            return None
    except ValueError:
        return None
    parts = relative.split(os.sep)
    if (
        len(parts) < 3
        or parts[-1] != "session.jsonl"
        or _ASYNC_CHILD_ID.fullmatch(parts[0]) is None
    ):
        return None
    # The selected session's ancestors may have platform aliases (/var on macOS).
    # Reject aliases inside its child tree, where they could redirect a transcript.
    suffix = os.sep + relative
    if not value.endswith(suffix) or os.path.realpath(value[:-len(suffix)]) != root:
        return None
    return path, parts[0]


def _runtime_pi_header(value: dict) -> dict:
    return {
        name: value[name]
        for name in ("type", "version", "id", "cwd", "parentSession")
        if name in value
    }


def _pi_text(value: object) -> bool:
    if type(value) is str:
        return True
    return type(value) is list and all(
        type(block) is dict
        and _optional_type(block, "type", str)
        and _optional_type(block, "text", str)
        for block in value
    )


def _validate_pi_assistant(message: dict) -> None:
    content = message.get("content")
    if type(content) is not list or not all(type(block) is dict for block in content):
        raise ValueError("invalid pi assistant content")
    for block in content:
        if not _optional_type(block, "type", str):
            raise ValueError("invalid pi assistant block type")
        kind = block.get("type")
        if kind == "text" and not _optional_type(block, "text", str):
            raise ValueError("invalid pi assistant text")
        if kind == "thinking" and not _optional_type(block, "thinking", str):
            raise ValueError("invalid pi assistant thinking")
        if kind == "toolCall":
            if not all(
                _optional_type(block, field, str) for field in ("id", "name")
            ) or ("arguments" in block and type(block["arguments"]) is not dict):
                raise ValueError("invalid pi tool call")
            arguments = block.get("arguments", {})
            name = block.get("name") or ""
            validate_recognizer_inputs(name, arguments)
            known_fields = {
                "subagent": ("task", "agent"),
                "bash": ("command",),
                "read": ("path",),
                "edit": ("path",),
                "write": ("path",),
                "grep": ("pattern", "path"),
            }.get(name, ())
            if not all(
                _optional_type(arguments, field, str) for field in known_fields
            ):
                raise ValueError("invalid pi tool arguments")
    usage = message.get("usage")
    if usage is not None and (
        type(usage) is not dict or not _optional_type(usage, "output", int)
    ):
        raise ValueError("invalid pi assistant usage")
    if not all(
        _optional_type(message, field, str)
        for field in ("model", "stopReason", "errorMessage")
    ):
        raise ValueError("invalid pi assistant fields")


def _validate_pi_child_result(result: object) -> None:
    if type(result) is not dict or not all(
        _optional_type(result, field, str)
        for field in ("agent", "task", "stopReason", "model", "sessionFile", "finalOutput")
    ) or not _optional_type(result, "exitCode", int):
        raise ValueError("invalid pi child result")
    messages = result.get("messages", [])
    if type(messages) is not list:
        raise ValueError("invalid pi child messages")
    for message in messages:
        _validate_pi_message(message, embedded=True)
    usage = result.get("usage")
    if usage is not None and (
        type(usage) is not dict or not _optional_type(usage, "output", int)
    ):
        raise ValueError("invalid pi child usage")


def _validate_pi_message(message: object, embedded: bool = False) -> None:
    if type(message) is not dict or not _optional_type(message, "role", str):
        raise ValueError("invalid pi message")
    role = message.get("role")
    if role == "assistant":
        _validate_pi_assistant(message)
    elif role == "user":
        if "content" in message and not _pi_text(message["content"]):
            raise ValueError("invalid pi user content")
    elif role == "toolResult":
        if not all(
            _optional_type(message, field, str)
            for field in ("toolCallId", "toolName")
        ) or not _optional_type(message, "isError", bool) or (
            "content" in message and not _pi_text(message["content"])
        ):
            raise ValueError("invalid pi tool result")
        details = message.get("details")
        if not embedded and type(details) is dict and "results" in details:
            results = details["results"]
            if type(results) is not list:
                raise ValueError("invalid pi child results")
            for result in results:
                _validate_pi_child_result(result)
    elif role == "bashExecution":
        if not all(
            _optional_type(message, field, str) for field in ("command", "output")
        ) or not _optional_type(message, "exitCode", int) or not _optional_type(
            message, "cancelled", bool,
        ):
            raise ValueError("invalid pi bash execution")
    elif role == "custom":
        if not _optional_type(message, "display", bool) or (
            "content" in message and not _pi_text(message["content"])
        ):
            raise ValueError("invalid pi custom message")


def _validate_pi_entry(entry: object, first: bool = False) -> None:
    if first:
        if not _valid_pi_header(entry):
            raise ValueError("invalid pi session header")
        return
    if type(entry) is not dict or not _optional_type(entry, "type", str):
        raise ValueError("invalid pi replacement record")
    if not _optional_type(entry, "timestamp", str):
        raise ValueError("invalid pi entry timestamp")
    kind = entry.get("type")
    if kind == "message":
        _validate_pi_message(entry.get("message"))
    elif kind == "model_change" and not _optional_type(entry, "modelId", str):
        raise ValueError("invalid pi model id")
    elif kind == "session_info" and not _optional_type(entry, "name", str):
        raise ValueError("invalid pi session name")
    elif kind in ("compaction", "branch_summary") and not _optional_type(
        entry, "summary", str,
    ):
        raise ValueError("invalid pi summary")


def pi_agent_dir() -> str:
    override = os.environ.get("PI_CODING_AGENT_DIR")
    if override:
        return os.path.expanduser(override)
    return os.path.expanduser("~/.pi/agent")


def pi_session_slug(cwd: str) -> str:
    """pi's directory name for a working directory: drop one leading separator,
    turn every remaining `/`, `\\`, and `:` into `-`, then wrap in `--`."""
    path = os.path.abspath(os.path.expanduser(cwd))
    return "--" + re.sub(r"[/\\:]", "-", re.sub(r"^[/\\]", "", path)) + "--"


def pi_session_dir(cwd: str) -> str:
    """Where pi keeps sessions for a working directory.

    PI_CODING_AGENT_SESSION_DIR replaces the whole location (pi's own
    `--session-dir`), so it is not encoded per working directory.
    """
    override = os.environ.get("PI_CODING_AGENT_SESSION_DIR")
    if override:
        return os.path.expanduser(override)
    return os.path.join(pi_agent_dir(), "sessions", pi_session_slug(cwd))


def _directory_entries(path: str) -> list[os.DirEntry]:
    try:
        with os.scandir(path) as stream:
            return list(stream)
    except OSError:
        return []


def _regular_jsonl_entries(entries: list[os.DirEntry]) -> list[os.DirEntry]:
    found = []
    for entry in entries:
        try:
            regular = entry.is_file(follow_symlinks=False)
        except OSError:
            continue
        if regular and entry.name.endswith(".jsonl"):
            found.append(entry)
    return found


def pi_session_files() -> list[str]:
    """Every pi session file on this machine.

    With PI_CODING_AGENT_SESSION_DIR set, all sessions share that one directory;
    otherwise each working directory has its own subdirectory of sessions/.
    """
    override = os.environ.get("PI_CODING_AGENT_SESSION_DIR")
    base = os.path.expanduser(override) if override else os.path.join(
        pi_agent_dir(), "sessions",
    )
    entries = _directory_entries(base)
    directories = [None]
    if not override:
        directories = []
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    directories.append(entry.path)
            except OSError:
                continue
    found = []
    for directory in directories:
        candidates = entries if directory is None else _directory_entries(directory)
        found.extend(entry.path for entry in _regular_jsonl_entries(candidates))
    return sorted(found)


def pi_header(path: str) -> dict:
    """The session header line, or {} when the file is not a pi session."""
    admission = read_admission(path, "pi")
    return (
        dict(admission.record)
        if admission is not None and admission.proof.provider == "pi" else {}
    )


def newest_pi_session(cwd: str) -> str | None:
    """Newest session file pi has written for a working directory."""
    directory = pi_session_dir(cwd)
    target_cwd = os.path.realpath(os.path.abspath(os.path.expanduser(cwd)))
    entries = _regular_jsonl_entries(_directory_entries(directory))
    best: tuple[float, str] | None = None
    for entry in entries:
        admission = read_admission(entry.path, "pi")
        if admission is None:
            continue
        header = admission.record
        header_cwd = header.get("cwd") if header else None
        if not isinstance(header_cwd, str) or not os.path.isabs(header_cwd):
            continue
        if os.path.realpath(header_cwd) != target_cwd:
            continue
        rank = admission.stamp.mtime_ns / 1_000_000_000
        if best is None or (rank, entry.path) > best:
            best = (rank, entry.path)
    return best[1] if best else None


def _text_of(content) -> str:
    """Flatten pi content — a bare string or a list of typed blocks."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    out = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            out.append(str(block.get("text") or ""))
    return "\n".join(part for part in out if part)


def _detail_of(arguments) -> str:
    if not isinstance(arguments, dict):
        return ""
    for key in _DETAIL_KEYS:
        value = arguments.get(key)
        if isinstance(value, str) and value:
            return one_line(os.path.basename(value.rstrip("/")) if key == "path" else value)
    return ""


class PiModel:
    """Incrementally tail one pi session file and maintain the node tree."""

    provider = PROVIDER

    def __init__(self, path: str):
        self.path = os.path.realpath(path)
        self._initialize({}, b"")

    def _initialize(self, header: dict, header_digest: bytes) -> None:
        self._header = header
        self._header_digest = header_digest
        self.cwd = self._header.get("cwd") or ""
        self.nodes: dict[str, Node] = {
            "main": Node(id="main", label="pi", detail=self.cwd[-DETAIL_CLIP:],
                         parent=None, status="running", kind="main")
        }
        self.task_list: dict = {}
        self._offset = 0
        self._carry = b""
        self._source_signature = None
        self._steps: dict[str, Step] = {}          # toolCallId -> its call step
        self._children = 0
        self._dispatches = 0
        self._async_runs: dict[str, dict] = {}
        self._async_sessions: dict[str, str] = {}
        self._async_seen: dict[str, set[str]] = {}
        self._changed = True
        self._replaying = False

    def reload_plan(self):
        optional = set()
        for run in self._async_runs.values():
            optional.add(os.path.join(run["dir"], "events.jsonl"))
        optional.update(self._async_sessions)
        return ReloadPlan(
            (self.path,),
            optional_files=tuple(sorted(optional)),
            followup=_pi_followup,
            admission_provider="pi",
        )

    def prepare_append(self, cohort: CapturedCohort) -> _PreparedPiAppend:
        main_source = cohort.file(self.path)
        if cohort.action == "bootstrap" and main_source is None:
            raise ValueError("missing pi source")
        header = None
        header_digest = b""
        if cohort.action == "bootstrap":
            if main_source is None or not main_source.records:
                raise ValueError("empty pi source")
            admission = cohort.admission_for(self.path)
            if (
                admission is None
                or admission.proof is None
                or admission.proof.provider != "pi"
            ):
                raise ValueError("invalid pi source header")
            header = _runtime_pi_header(admission.record)
            header_digest = admission.proof.raw_digest
        records = cohort.records_for(self.path)
        for entry in records:
            first = type(entry) is dict and entry.get("type") == "session"
            _validate_pi_entry(entry, first)
        child_records = []
        for path in sorted(self._async_sessions):
            source = cohort.file(path)
            if source is None:
                continue
            captured = cohort.records_for(path)
            if not self._valid_async_records(path, captured):
                continue
            child_records.append((path, captured))
        return _PreparedPiAppend(
            cohort.action,
            main_source,
            records,
            tuple(child_records),
            header,
            header_digest,
        )

    def apply_append(self, prepared: object) -> bool:
        batch = prepared
        if type(batch) is not _PreparedPiAppend:
            raise TypeError("invalid prepared pi append")
        prior_nodes = self.nodes
        prior_tasks = self.task_list
        if batch.action == "bootstrap":
            self._initialize(batch.header or {}, batch.header_digest)
        changed = batch.action == "bootstrap"
        for entry in batch.records:
            changed |= self._ingest(entry)
        for path, records in batch.child_records:
            changed |= self._ingest_async_session(path, records)
        changed |= self._reconcile_async_artifacts()
        if batch.source is not None:
            self._offset = batch.source.offset
            self._carry = b""
            self._source_signature = batch.source.signature
        self._changed = False
        changed |= sync_batch_statuses(self.nodes)
        if batch.action == "bootstrap":
            replacement_nodes = self.nodes
            prior_main = prior_nodes["main"]
            prior_main.__dict__.update(replacement_nodes["main"].__dict__)
            replacement_nodes["main"] = prior_main
            prior_nodes.clear()
            prior_nodes.update(replacement_nodes)
            self.nodes = prior_nodes
            prior_tasks.clear()
            prior_tasks.update(self.task_list)
            self.task_list = prior_tasks
        return changed

    def settle_idle(self) -> bool:
        changed = self._reconcile_async_artifacts()
        changed |= sync_batch_statuses(self.nodes)
        return changed

    # ---- provider-model surface -------------------------------------------
    def poll(self) -> bool:
        return poll_append(self)

    @property
    def source_revision(self) -> int:
        return _source_revision(self)

    @property
    def visible_revision(self) -> int:
        return _visible_revision(self)

    def observation_paths(self) -> set[str]:
        paths = {self.path, os.path.dirname(self.path)}
        paths.update(run["dir"] for run in self._async_runs.values())
        for run in self._async_runs.values():
            paths.update(run["child_status_dirs"])
        paths.update(self._async_sessions)
        return paths

    def accepts_observation(self, paths) -> bool:
        roots = self.observation_paths()
        for changed in paths:
            candidate = os.path.realpath(os.path.abspath(changed))
            for root in roots:
                canonical = os.path.realpath(root)
                try:
                    if candidate == canonical or os.path.commonpath(
                        (candidate, canonical),
                    ) == canonical:
                        return True
                except ValueError:
                    continue
        return False

    def lifecycle_identity(self):
        # pi writes no lifecycle hook journal; state comes from the transcript.
        return None

    # ---- ingest ------------------------------------------------------------
    def _ingest(self, entry: dict) -> bool:
        if not isinstance(entry, dict):
            return False
        kind = entry.get("type")
        ts = parse_timestamp(entry.get("timestamp"))
        if kind == "message":
            message = entry.get("message")
            if not isinstance(message, dict):
                return False
            return self._message(message, ts)
        node = self.nodes["main"]
        if kind == "model_change":
            name = str(entry.get("modelId") or "")
            if not name or node.model == name:
                return False
            node.model = name
            return True
        if kind == "session_info":
            name = str(entry.get("name") or "")
            if not name or node.detail == name[:DETAIL_CLIP]:
                return False
            node.detail = name[:DETAIL_CLIP]
            return True
        if kind in ("compaction", "branch_summary"):
            summary = str(entry.get("summary") or "")
            if not summary:
                return False
            title = "compaction" if kind == "compaction" else "branch summary"
            self._add(node, Step("text", f"{title}: " + one_line(summary),
                                 summary[:BODY_CLIP], ts))
            return True
        return False

    def _message(self, message: dict, ts: float) -> bool:
        role = message.get("role")
        node = self.nodes["main"]
        if role == "user":
            text = _text_of(message.get("content")).strip()
            if not text:
                return False
            self._add(node, Step("prompt", "task: " + one_line(text)[:DETAIL_CLIP],
                                 text[:BODY_CLIP], ts))
            node.status = "running"
            return True
        if role == "assistant":
            return self._assistant(node, message, ts)
        if role == "toolResult":
            return self._tool_result(message)
        if role == "bashExecution":
            return self._bash_execution(node, message, ts)
        if role == "custom" and message.get("display"):
            text = _text_of(message.get("content")).strip()
            if not text:
                return False
            self._add(node, Step("text", "text: " + one_line(text), text[:BODY_CLIP], ts))
            return True
        return False

    def _assistant(self, node: Node, message: dict, ts: float) -> bool:
        changed = False
        for block in message.get("content") or []:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                text = str(block.get("text") or "").strip()
                if text:
                    self._add(node, Step("text", "text: " + one_line(text),
                                         text[:BODY_CLIP], ts))
                    node.result = text[:RESULT_CLIP]
                    changed = True
            elif btype == "thinking":
                text = str(block.get("thinking") or "").strip()
                if text:
                    self._add(node, Step("thinking", "thinking: " + one_line(text),
                                         text[:BODY_CLIP], ts))
                    changed = True
            elif btype == "toolCall":
                self._tool_call(node, block, ts)
                changed = True
        usage = message.get("usage")
        if isinstance(usage, dict):
            node.tokens += int(usage.get("output") or 0)
        model = str(message.get("model") or "")
        if model and node.model != model:
            node.model = model
        stop = str(message.get("stopReason") or "")
        if stop in _FAILED_STOP:
            node.status = "error"
            reason = str(message.get("errorMessage") or "").strip()
            if reason:
                node.result = reason[:RESULT_CLIP]
        elif stop in _ACTIVE_STOP:
            node.status = "running"
        elif stop:
            node.status = "waiting"
        return changed or bool(stop)

    def _tool_call(self, node: Node, block: dict, ts: float, register: bool = True) -> None:
        name = str(block.get("name") or "tool")
        arguments = block.get("arguments")
        arguments = arguments if isinstance(arguments, dict) else {}
        skill = skill_mode.skill_of(name, arguments)
        if skill and (not node.skills or node.skills[-1] != skill):
            node.skills.append(skill)
        workflow.note(node, name, arguments, skill)
        detail = skill or _detail_of(arguments)
        spawn = name == SPAWN_TOOL
        step = Step("spawn" if spawn else "tool",
                    clip_text(f"{name}: {detail}" if detail else name, 78),
                    json.dumps(arguments, indent=2)[:BODY_CLIP], ts,
                    status="running", tid=str(block.get("id") or ""),
                    goal=preferred_goal(arguments.get("task")) if spawn else "")
        self._add(node, step)
        if register and step.tid:
            self._steps[step.tid] = step

    def _tool_result(self, message: dict) -> bool:
        step = self._steps.get(str(message.get("toolCallId") or ""))
        if step is None:
            return self._apply_async_completions(message.get("details"))
        text = _text_of(message.get("content")).strip()
        if text:
            step.output = text[:BODY_CLIP]
        async_run = False
        if step.kind == "spawn":
            async_run = self._register_async_run(step, message.get("details"))
            if not async_run:
                self._spawn_children(step, message.get("details"))
        if not async_run:
            step.status = "error" if message.get("isError") else "done"
        self._apply_async_completions(message.get("details"))
        return True

    @staticmethod
    def _async_dir(run_id: str, value: object) -> str | None:
        if (
            type(value) is not str
            or not os.path.isabs(value)
            or os.path.normpath(value) != os.path.abspath(value)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", run_id)
            or ".." in run_id
        ):
            return None
        path = os.path.realpath(value)
        parent = os.path.dirname(path)
        scope = os.path.basename(os.path.dirname(parent))
        temp_root = os.path.realpath(tempfile.gettempdir())
        try:
            under_temp = os.path.commonpath((path, temp_root)) == temp_root
        except ValueError:
            under_temp = False
        if (
            os.path.basename(path) != run_id
            or os.path.basename(parent) != "async-subagent-runs"
            or not scope.startswith("pi-subagents-")
            or not under_temp
        ):
            return None
        return path

    def _register_async_run(self, step: Step, details: object) -> bool:
        if type(details) is not dict:
            return False
        run_id = details.get("runId") or details.get("asyncId") or details.get("id")
        if type(run_id) is not str:
            return False
        directory = self._async_dir(run_id, details.get("asyncDir"))
        if directory is None:
            return False
        declared_call = details.get("toolCallId")
        if type(declared_call) is str and declared_call != step.tid:
            return False
        existing = self._async_runs.get(run_id)
        if existing is not None:
            return existing["dir"] == directory and existing["tid"] == step.tid
        self._async_runs[run_id] = {
            "dir": directory,
            "tid": step.tid,
            "seen_status": False,
            "terminal": False,
            "children": [],
            "child_status_dirs": [],
        }
        step.status = "running"
        return True

    @staticmethod
    def _read_bounded_json(path: str) -> object | None:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError:
            return None
        try:
            before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_size < 2
                or before.st_size > _ASYNC_STATUS_LIMIT
            ):
                return None
            chunks = []
            remaining = before.st_size
            while remaining:
                chunk = os.read(descriptor, min(remaining, 64 * 1024))
                if not chunk:
                    return None
                chunks.append(chunk)
                remaining -= len(chunk)
            after = os.fstat(descriptor)
            if (
                (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            ):
                return None
            return strict_json_record(b"".join(chunks))
        except (OSError, ValueError):
            return None
        finally:
            os.close(descriptor)

    def _status_for(self, run_id: str, run: dict) -> dict | None:
        value = self._read_bounded_json(os.path.join(run["dir"], "status.json"))
        if type(value) is not dict:
            return None
        identity = value.get("runId") or value.get("id")
        if identity != run_id:
            return None
        session_id = value.get("sessionId")
        if type(session_id) is not str:
            return None
        parent_ids = {self.path, self._header.get("id")}
        if session_id not in parent_ids:
            if not (
                os.path.isabs(session_id)
                and os.path.realpath(session_id) == self.path
            ):
                return None
        declared_dir = value.get("asyncDir")
        if declared_dir is not None and self._async_dir(run_id, declared_dir) != run["dir"]:
            return None
        steps = value.get("steps", [])
        if type(steps) is not list or len(steps) > 2048:
            return None
        if not all(type(item) is dict for item in steps):
            return None
        return value

    @staticmethod
    def _async_status(value: object, prior: str = "waiting") -> str:
        if value in {"queued", "pending", "paused"}:
            return "waiting"
        if value == "running":
            return "running"
        if value in {"complete", "completed"}:
            return "done"
        if value in _ASYNC_ERROR:
            return "error"
        return prior

    def _async_node(
        self, run_id: str, run: dict, index: int, spec: dict,
    ) -> tuple[Node, bool]:
        children = run["children"]
        changed = False
        while len(children) <= index:
            nid = "pi-async:%s:%d" % (run_id, len(children) + 1)
            children.append(nid)
            self.nodes[nid] = Node(
                id=nid,
                label="agent",
                parent="main",
                status="waiting",
                kind="agent",
            )
            self.nodes["main"].children.append(nid)
            changed = True
        node = self.nodes[children[index]]
        label = spec.get("label") or spec.get("workflowKey") or spec.get("agent")
        wanted_label = label[:DETAIL_CLIP] if type(label) is str and label else node.label
        if node.label != wanted_label:
            node.label = wanted_label
            changed = True
        role = spec.get("agent")
        detail = spec.get("task") or (
            role if type(role) is str and role != wanted_label else None
        )
        wanted_detail = (
            one_line(detail)[:DETAIL_CLIP]
            if type(detail) is str and detail else node.detail
        )
        if node.detail != wanted_detail:
            node.detail = wanted_detail
            changed = True
        model = spec.get("model")
        wanted_model = model if type(model) is str and model else node.model
        if node.model != wanted_model:
            node.model = wanted_model
            changed = True
        wanted_status = self._async_status(spec.get("status"), node.status)
        if node.status != wanted_status:
            node.status = wanted_status
            changed = True
        result = spec.get("error") if wanted_status == "error" else spec.get("result")
        wanted_result = result[:RESULT_CLIP] if type(result) is str and result else node.result
        if node.result != wanted_result:
            node.result = wanted_result
            changed = True
        return node, changed

    def _attach_async_children(self, run_id: str, run: dict, specs: list[dict]) -> bool:
        changed = False
        for index, spec in enumerate(specs):
            node, node_changed = self._async_node(run_id, run, index, spec)
            changed |= node_changed
            path = spec.get("sessionFile")
            child_run = spec.get("runId")
            directory = self._child_status_directory(child_run, run_id)
            if directory is not None and directory not in run["child_status_dirs"]:
                run["child_status_dirs"].append(directory)
                changed = True
            valid_path = self._child_session_path(path, child_run, run_id)
            if valid_path is not None:
                if self._async_sessions.get(valid_path) != node.id:
                    self._async_sessions[valid_path] = node.id
                    changed = True
                self._async_seen.setdefault(valid_path, set())
        step = self._steps.get(run["tid"])
        children = run["children"]
        if step is not None and children:
            if step.child != children[0]:
                step.child = children[0]
                changed = True
            if len(children) > 1:
                batch_id = "dispatch:main:%s" % (step.tid or run_id)
                batch = self.nodes.get(batch_id)
                if batch is None:
                    batch = self.nodes[batch_id] = Node(
                        id=batch_id,
                        label=batch_label(
                            (self.nodes[nid].detail for nid in children),
                            self._dispatches + 1,
                        ),
                        parent="main",
                        status="running",
                        kind="dispatch",
                    )
                    self._dispatches += 1
                    positions = [
                        self.nodes["main"].children.index(nid)
                        for nid in children if nid in self.nodes["main"].children
                    ]
                    self.nodes["main"].children.insert(
                        min(positions) if positions else len(self.nodes["main"].children),
                        batch_id,
                    )
                    changed = True
                if step.dispatch != batch_id:
                    step.dispatch = batch_id
                    changed = True
                for nid in children:
                    if nid in self.nodes["main"].children:
                        self.nodes["main"].children.remove(nid)
                        changed = True
                    if nid not in batch.children:
                        batch.children.append(nid)
                        changed = True
                    if self.nodes[nid].parent != batch_id:
                        self.nodes[nid].parent = batch_id
                        changed = True
        return changed

    def _child_status_directory(self, run_id: object, workflow_run: str | None) -> str | None:
        run = self._async_runs.get(workflow_run)
        if run is None or type(run_id) is not str:
            return None
        directory = os.path.join(os.path.dirname(run["dir"]), run_id)
        return directory if self._async_dir(run_id, directory) == directory else None

    def _child_session_path(
        self, value: object, run_id: object, workflow_run: str | None = None,
    ) -> str | None:
        identity = _async_child_identity(self.path, value)
        if identity is None:
            return None
        path, child_run = identity
        if run_id is not None and (
            type(run_id) is not str
            or _ASYNC_CHILD_ID.fullmatch(run_id) is None
        ):
            return None
        if run_id is not None and run_id != child_run:
            directory = self._child_status_directory(run_id, workflow_run)
            if directory is None:
                return None
            status = self._status_for(run_id, {"dir": directory})
            if (
                status is None
                or status.get("parentWorkflowRunId") != workflow_run
            ):
                return None
            session_files = [status.get("sessionFile")]
            session_files.extend(spec.get("sessionFile") for spec in status.get("steps", []))
            if not any(
                _async_child_identity(self.path, source) == identity
                for source in session_files
            ):
                return None
        admission = read_admission(path, "pi")
        if (
            admission is None
            or admission.path != path
            or admission.proof.provider != "pi"
        ):
            return None
        return path

    def _reconcile_async_artifacts(self) -> bool:
        changed = False
        for run_id, run in self._async_runs.items():
            status = self._status_for(run_id, run)
            if status is None:
                if run["seen_status"] and not run["terminal"] and not os.path.isdir(run["dir"]):
                    changed |= self._finish_async_run(run, "error")
                continue
            run["seen_status"] = True
            specs = status.get("steps") or []
            changed |= self._attach_async_children(run_id, run, specs)
            state = status.get("state")
            if state in _ASYNC_TERMINAL:
                changed |= self._finish_async_run(
                    run,
                    "waiting" if state == "paused" else (
                        "error" if state in _ASYNC_ERROR else "done"
                    ),
                )
            elif not run["terminal"]:
                step = self._steps.get(run["tid"])
                if step is not None and step.status != "running":
                    step.status = "running"
                    changed = True
        return changed

    def _finish_async_run(self, run: dict, status: str) -> bool:
        changed = not run["terminal"]
        run["terminal"] = True
        children = [self.nodes[nid] for nid in run["children"] if nid in self.nodes]
        for child in children:
            if child.status in {"running", "waiting"} and child.status != status:
                child.status = status
                changed = True
        effective = "error" if any(child.status == "error" for child in children) else status
        step = self._steps.get(run["tid"])
        if step is not None and step.status != effective:
            step.status = effective
            changed = True
        return changed

    def _apply_async_completions(self, details: object) -> bool:
        completions = details.get("completions") if type(details) is dict else None
        if type(completions) is not list:
            return False
        changed = False
        for completion in completions:
            if type(completion) is not dict:
                continue
            run_id = completion.get("runId") or completion.get("id")
            run = self._async_runs.get(run_id)
            if run is None:
                continue
            results = completion.get("results")
            state = completion.get("state")
            if type(results) is list:
                specs = []
                for result in results[:2048]:
                    if type(result) is not dict:
                        continue
                    spec = dict(result)
                    spec["status"] = (
                        "paused" if state == "paused" else (
                            "completed" if result.get("success") else "failed"
                        )
                    )
                    specs.append(spec)
                changed |= self._attach_async_children(run_id, run, specs)
            success = completion.get("success")
            failed = success is False or state in _ASYNC_ERROR
            terminal = "waiting" if state == "paused" else ("error" if failed else "done")
            changed |= self._finish_async_run(run, terminal)
        return changed

    def _valid_async_records(self, path: str, records: tuple[dict, ...]) -> bool:
        if not records:
            return True
        first_capture = not self._async_seen.get(path)
        if first_capture and records[0].get("type") != "session":
            return False
        try:
            for index, entry in enumerate(records):
                _validate_pi_entry(
                    entry,
                    first_capture and index == 0 and entry.get("type") == "session",
                )
        except (AttributeError, ValueError):
            return False
        return True

    @staticmethod
    def _async_record_key(entry: dict) -> str:
        entry_id = entry.get("id")
        if type(entry_id) is str and entry_id:
            return "id:" + entry_id
        return "value:" + json.dumps(entry, sort_keys=True, separators=(",", ":"))

    def _ingest_async_session(self, path: str, records: tuple[dict, ...]) -> bool:
        node = self.nodes.get(self._async_sessions.get(path, ""))
        if node is None:
            return False
        seen = self._async_seen.setdefault(path, set())
        changed = False
        for entry in records:
            key = self._async_record_key(entry)
            if key in seen:
                continue
            seen.add(key)
            kind = entry.get("type")
            ts = parse_timestamp(entry.get("timestamp"))
            if kind == "session":
                continue
            if kind == "model_change":
                model = entry.get("modelId")
                if type(model) is str and model and node.model != model:
                    node.model = model
                    changed = True
                continue
            if kind != "message":
                continue
            message = entry.get("message")
            if type(message) is not dict:
                continue
            role = message.get("role")
            if role == "user":
                text = _text_of(message.get("content")).strip()
                if text:
                    self._add(node, Step(
                        "prompt",
                        "task: " + one_line(text)[:DETAIL_CLIP],
                        text[:BODY_CLIP],
                        ts,
                    ))
                    if not node.detail:
                        node.detail = one_line(text)[:DETAIL_CLIP]
                    changed = True
            elif role == "assistant":
                before = len(node.steps)
                self._assistant_into(node, message, ts)
                changed |= len(node.steps) != before
            elif role == "toolResult":
                before = tuple((step.status, step.output) for step in node.steps)
                self._replay(node, message, ts)
                changed |= before != tuple((step.status, step.output) for step in node.steps)
            elif role == "bashExecution":
                changed |= self._bash_execution(node, message, ts)
            elif role == "custom" and message.get("display"):
                text = _text_of(message.get("content")).strip()
                if text:
                    self._add(node, Step(
                        "text", "text: " + one_line(text), text[:BODY_CLIP], ts,
                    ))
                    changed = True
        return changed

    def _bash_execution(self, node: Node, message: dict, ts: float) -> bool:
        command = str(message.get("command") or "").strip()
        if not command:
            return False
        exit_code = message.get("exitCode")
        failed = bool(message.get("cancelled")) or (
            isinstance(exit_code, int) and exit_code != 0)
        step = Step("tool", clip_text("bash: " + one_line(command), 78), command[:BODY_CLIP],
                    ts, status="error" if failed else "done")
        step.output = str(message.get("output") or "")[:BODY_CLIP]
        self._add(node, step)
        return True

    def _spawn_children(self, step: Step, details) -> None:
        """Read current child sessions or replay older embedded transcripts."""
        results = details.get("results") if isinstance(details, dict) else None
        if not isinstance(results, list):
            return
        children = []
        for result in results:
            if not isinstance(result, dict):
                continue
            self._children += 1
            nid = "pi:%d" % self._children
            label = str(result.get("agent") or "agent")
            task = str(result.get("task") or "")
            stop = str(result.get("stopReason") or "")
            exit_code = result.get("exitCode")
            failed = stop in _FAILED_STOP or (isinstance(exit_code, int) and exit_code != 0)
            child = Node(id=nid, label=label, detail=one_line(task)[:DETAIL_CLIP],
                         parent="main", status="error" if failed else "done",
                         kind="agent", model=str(result.get("model") or ""),
                         last_ts=step.ts)
            messages = result.get("messages") or []
            path = None if messages else self._child_session_path(
                result.get("sessionFile"), None,
            )
            if task and path is None:
                child.steps.append(Step("prompt", "task: " + one_line(task)[:DETAIL_CLIP],
                                        task[:BODY_CLIP], step.ts))
            for message in messages:
                if isinstance(message, dict):
                    self._replay(child, message, step.ts)
            if path is not None:
                self._async_sessions[path] = nid
                self._async_seen.setdefault(path, set())
            elif not messages:
                output = result.get("finalOutput")
                if type(output) is str and output.strip():
                    self._replay(child, {"role": "assistant", "content": [
                        {"type": "text", "text": output},
                    ]}, step.ts)
                usage = result.get("usage")
                if type(usage) is dict:
                    child.tokens = usage.get("output") or 0
            self.nodes[nid] = child
            self.nodes["main"].children.append(nid)
            children.append(nid)
            if not step.child:
                step.child = nid
        if len(children) > 1:
            self._group_dispatch(step, children)

    def _group_dispatch(self, step: Step, children: list[str]) -> None:
        """Place one multi-agent tool result behind the shared dispatch row."""
        self._dispatches += 1
        batch_id = "dispatch:main:%s" % (step.tid or self._dispatches)
        batch = Node(id=batch_id, label=batch_label(
            (self.nodes[child_id].detail for child_id in children), self._dispatches),
                     parent="main", status="running", kind="dispatch")
        self.nodes[batch_id] = batch
        parent = self.nodes["main"]
        positions = [parent.children.index(child) for child in children
                     if child in parent.children]
        parent.children.insert(min(positions) if positions else len(parent.children), batch_id)
        step.dispatch = batch_id
        for child_id in children:
            if child_id in parent.children:
                parent.children.remove(child_id)
            batch.children.append(child_id)
            self.nodes[child_id].parent = batch_id

    def _replay(self, child: Node, message: dict, ts: float) -> None:
        """Render one embedded child message as an action on the child node."""
        role = message.get("role")
        if role == "assistant":
            self._assistant_into(child, message, ts)
        elif role == "toolResult":
            text = _text_of(message.get("content")).strip()
            for step in reversed(child.steps):
                if step.tid == str(message.get("toolCallId") or ""):
                    step.output = text[:BODY_CLIP]
                    step.status = "error" if message.get("isError") else "done"
                    break

    def _assistant_into(self, child: Node, message: dict, ts: float) -> None:
        for block in message.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                text = str(block.get("text") or "").strip()
                if text:
                    child.steps.append(Step("text", "text: " + one_line(text),
                                            text[:BODY_CLIP], ts))
                    child.result = text[:RESULT_CLIP]
            elif block.get("type") == "thinking":
                text = str(block.get("thinking") or "").strip()
                if text:
                    child.steps.append(Step("thinking", "thinking: " + one_line(text),
                                            text[:BODY_CLIP], ts))
            elif block.get("type") == "toolCall":
                self._tool_call(child, block, ts, register=False)
        usage = message.get("usage")
        if isinstance(usage, dict):
            child.tokens += int(usage.get("output") or 0)
        model = message.get("model")
        if isinstance(model, str) and model:
            child.model = model

    @staticmethod
    def _add(node: Node, step: Step) -> None:
        node.steps.append(step)
        node.last_ts = max(node.last_ts, step.ts)
