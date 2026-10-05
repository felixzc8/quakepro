"""Codex rollout adapter for QuakePro's provider-neutral node model."""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import skill_mode
from . import shell
from . import workflow
from .batch_labels import batch_label
from .batch_model import sync_batch_statuses
from .provider_admission import AdmissionProof
from .provider_reload import (
    CapturedCohort,
    DirectoryRule,
    ReloadPlan,
    advance_visible_revision as _advance_visible_revision,
    accepts_observation as _reload_accepts_observation,
    poll_append,
    source_revision as _source_revision,
    visible_revision as _visible_revision,
)
from .provider_schema import (
    optional_exact_type as _optional_exact_type,
    optional_type as _optional_type,
    validate_recognizer_inputs,
)
from .session_model import (
    BODY_CLIP,
    DETAIL_CLIP,
    RESULT_CLIP,
    LifecycleFact,
    Node,
    Step,
    clip_text,
    one_line,
    parse_timestamp,
)
from .session_identity import (
    internal_codex_subagent as _internal_subagent,
    sessions_root,
    _valid_codex_meta,
)
from .source_cohort import (
    DirectoryStamp,
    _finite_json_value,
    _stamp_tuple,
)


FAILED = {"aborted", "cancelled", "error", "failed", "incomplete", "interrupted"}
ENCRYPTED_TOKEN = re.compile(r"\b(?:gAAAAA|eyJjaXBoZXJ0ZXh0I)[A-Za-z0-9_-]{20,}={0,2}")
# Codex funnels every real tool through one `exec` tool whose input is a JS snippet
# like `await tools.exec_command({cmd:...})`; these recover the inner call.
_INNER_CALL = re.compile(r"tools\.(\w+)\s*\(")
_PATCH_FILE = re.compile(r"\*\*\* (?:Add|Update|Delete) File: (.+)")
_PLAN_STEP = re.compile(
    r'"?step"?\s*:\s*"((?:\\.|[^"])*)"\s*,\s*"?status"?\s*:\s*"in_progress"'
    r'|"?status"?\s*:\s*"in_progress"\s*,\s*"?step"?\s*:\s*"((?:\\.|[^"])*)"')
_JS_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "'": "'",
               "`": "`", "\\": "\\", "/": "/"}
_EXEC_KEYS = ("cmd", "command", "q", "query", "pattern", "url", "title",
              "file_path", "path")
_REGEX_LEADING_TOKENS = frozenset("=([{,:;!?&|+-*%~")
_REGEX_LEADING_KEYWORDS = frozenset(("return", "throw", "case", "yield"))
_SHELL_TOOLS = frozenset(("bash", "exec_command", "shell"))


def _sanitize_text(value: str) -> str:
    return ENCRYPTED_TOKEN.sub("", value)


def _plain_text(value) -> str:
    if isinstance(value, str):
        return _sanitize_text(value)
    if isinstance(value, list):
        return "\n".join(filter(None, (_plain_text(item) for item in value)))
    if not isinstance(value, dict):
        return ""
    if value.get("type") in ("encrypted_content", "reasoning_encrypted"):
        return ""
    for key in ("text", "output_text", "input_text", "message"):
        text = value.get(key)
        if isinstance(text, str):
            return _sanitize_text(text)
    for key in ("content", "output", "summary"):
        if key in value:
            return _plain_text(value[key])
    return ""


def _safe_value(value):
    if isinstance(value, str):
        return _sanitize_text(value)
    if isinstance(value, list):
        return [_safe_value(item) for item in value]
    if isinstance(value, dict):
        if value.get("type") in ("encrypted_content", "reasoning_encrypted"):
            return {}
        return {key: _safe_value(item) for key, item in value.items()
                if key not in ("encrypted_content", "ciphertext")}
    return value


def _reject_constant(constant: str):
    raise ValueError("non-finite Codex tool argument: " + constant)


def _call_input(payload: dict):
    raw = payload.get("arguments") if payload.get("type") == "function_call" else payload.get("input")
    if not isinstance(raw, str):
        return _safe_value(raw), ""
    try:
        value = json.loads(raw, parse_constant=_reject_constant)
        if not _finite_json_value(value):
            raise ValueError("invalid Codex tool arguments")
        return _safe_value(value), raw
    except json.JSONDecodeError:
        if payload.get("type") == "function_call":
            raise ValueError("invalid Codex function arguments")
        return _safe_value(raw), raw


def _body_for(value, raw: str = "") -> str:
    if isinstance(value, str):
        return value[:BODY_CLIP]
    if value is None:
        return ""
    try:
        return json.dumps(value, indent=2)[:BODY_CLIP]
    except (TypeError, ValueError):
        return raw[:BODY_CLIP]


def _js_unescape(text: str) -> str:
    return re.sub(r"\\(.)", lambda m: _JS_ESCAPES.get(m.group(1), m.group(1)), text)


@dataclass(frozen=True)
class _JSScan:
    brace_depth: int
    parens: tuple[tuple[str, int], ...]
    statement: tuple[str, ...]


def _scan_js(text: str, index: int, *, collect_statement: bool = False) -> _JSScan | None:
    quote = ""
    escaped = False
    regex = False
    regex_class = False
    line_comment = False
    block_comment = False
    depth = 0
    parens: list[tuple[str, int]] = []
    statement: list[str] = []
    word = ""
    last_token = ""
    cursor = 0
    while cursor < index:
        char = text[cursor]
        following = text[cursor + 1] if cursor + 1 < len(text) else ""
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
                last_token = "value"
                if collect_statement:
                    statement.append(last_token)
        elif regex:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == "[":
                regex_class = True
            elif char == "]":
                regex_class = False
            elif char == "/" and not regex_class:
                regex = False
                last_token = "value"
                if collect_statement:
                    statement.append(last_token)
        elif line_comment:
            if char == "\n":
                line_comment = False
        elif block_comment:
            if char == "*" and following == "/":
                block_comment = False
                cursor += 1
        else:
            if char.isalnum() or char in "_$":
                word += char
                cursor += 1
                continue
            if word:
                last_token = word
                if collect_statement:
                    statement.append(word)
                word = ""
            if char == "/" and following == "/":
                line_comment = True
                cursor += 1
            elif char == "/" and following == "*":
                block_comment = True
                cursor += 1
            elif char == "/" and (last_token in _REGEX_LEADING_TOKENS
                                   or last_token in _REGEX_LEADING_KEYWORDS):
                regex = True
                regex_class = False
                escaped = False
            elif char in "\"'`":
                quote = char
            elif char == "{":
                depth += 1
                last_token = char
                if collect_statement:
                    statement.append(char)
            elif char == "}":
                depth -= 1
                last_token = char
                if collect_statement:
                    if depth == 0:
                        statement.clear()
                    else:
                        statement.append(char)
            elif char == "(":
                parens.append(("for" if last_token == "for" else "", 0))
                last_token = char
                if collect_statement:
                    statement.append(char)
            elif char == ")":
                if not parens:
                    return None
                parens.pop()
                last_token = char
                if collect_statement:
                    statement.append(char)
            elif not char.isspace():
                if char == ";" and parens and parens[-1][0] == "for":
                    kind, phase = parens[-1]
                    parens[-1] = kind, phase + 1
                last_token = char
                if collect_statement:
                    if char == ";" and depth == 0 and not parens:
                        statement.clear()
                    else:
                        statement.append(char)
        cursor += 1
    if word and collect_statement:
        statement.append(word)
    if quote or regex or line_comment or block_comment or depth < 0:
        return None
    return _JSScan(depth, tuple(parens), tuple(statement))


def _js_context_at(text: str, index: int) -> tuple[int, int] | None:
    scan = _scan_js(text, index)
    if scan is None:
        return None
    return scan.brace_depth, len(scan.parens)


def _js_depth_at(text: str, index: int) -> int | None:
    context = _js_context_at(text, index)
    return context[0] if context is not None else None


def _js_code_at(text: str, index: int) -> bool:
    return _js_depth_at(text, index) is not None


def _js_string(text: str, key: str) -> str:
    """Read a string literal assigned to `key` in a JS object, tolerating quoted or
    bare keys and single/double/backtick delimiters."""
    matches = re.finditer(
        rf'(?<!\w)["\']?{re.escape(key)}["\']?\s*:\s*(["\'`])((?:\\.|(?!\1).)*)\1',
        text, re.DOTALL)
    found = [candidate for candidate in matches
             if _js_code_at(text, candidate.start())]
    if len(found) != 1 or (found[0].group(1) == "`" and "${" in found[0].group(2)):
        return ""
    return _js_unescape(found[0].group(2))


def _call_args(text: str, start: int) -> str | None:
    depth = 1
    quote = ""
    escaped = False
    line_comment = False
    block_comment = False
    index = start
    while index < len(text):
        char = text[index]
        following = text[index + 1] if index + 1 < len(text) else ""
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
        elif line_comment:
            if char == "\n":
                line_comment = False
        elif block_comment:
            if char == "*" and following == "/":
                block_comment = False
                index += 1
        elif char == "/" and following == "/":
            line_comment = True
            index += 1
        elif char == "/" and following == "*":
            block_comment = True
            index += 1
        elif char in "\"'`":
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return text[start:index]
        index += 1
    return None


def _assigned_js_string(text: str, key: str) -> str:
    pattern = re.compile(
        rf'(?<![\w.$])(?:(?P<declaration>const|let|var)\s+)?'
        rf'{re.escape(key)}\s*=',
    )
    matches = []
    for match in pattern.finditer(text):
        scan = _scan_js(text, match.start())
        if scan is None or scan.brace_depth != 0:
            continue
        same_scope = not scan.parens
        for_initializer = (not match.group("declaration")
                           and bool(scan.parens)
                           and scan.parens[0] == ("for", 0)
                           and all(kind == "" for kind, _ in scan.parens[1:]))
        if same_scope or for_initializer:
            matches.append((match, len(scan.parens) - 1 if for_initializer else 0))
    if not matches:
        return ""
    match, closing_groups = matches[-1]
    tail = text[match.end():]
    literal = re.match(r'\s*(["\'`])((?:\\.|(?!\1).)*)\1', tail, re.DOTALL)
    if not literal or (literal.group(1) == "`" and "${" in literal.group(2)):
        return ""
    suffix = tail[literal.end():]
    for _ in range(closing_groups):
        closing = re.match(r"\s*\)", suffix)
        if not closing:
            return ""
        suffix = suffix[closing.end():]
    leading = re.match(r"\s*", suffix).group(0)
    if "\n" not in leading and suffix[len(leading):len(leading) + 1] not in ("", ";"):
        return ""
    return _js_unescape(literal.group(2))


def _shell_command(text: str, args: str, call_start: int) -> str:
    for key in ("cmd", "command"):
        value = _js_string(args, key)
        if value:
            return value
    shorthand = re.fullmatch(r"\s*\{\s*(cmd|command)\s*\}\s*", args)
    if not shorthand:
        return ""
    return _assigned_js_string(text[:call_start], shorthand.group(1))


def _literal_tokens(tokens: list[str], value: str) -> bool:
    while len(tokens) >= 2 and tokens[0] == "(" and tokens[-1] == ")":
        depth = 0
        closes_at_end = False
        for position, token in enumerate(tokens):
            if token == "(":
                depth += 1
            elif token == ")":
                depth -= 1
                if depth == 0:
                    closes_at_end = position == len(tokens) - 1
                    break
        if not closes_at_end:
            break
        tokens = tokens[1:-1]
    return tokens == [value]


def _js_call_is_skipped(statement: tuple[str, ...]) -> bool:
    tokens = list(statement)
    while len(tokens) >= 2 and tokens[:2] == ["if", "("]:
        depth = 1
        closing = -1
        for position in range(2, len(tokens)):
            if tokens[position] == "(":
                depth += 1
            elif tokens[position] == ")":
                depth -= 1
                if depth == 0:
                    closing = position
                    break
        if closing < 0:
            break
        if not _literal_tokens(tokens[2:closing], "true"):
            return True
        tokens = tokens[closing + 1:]
    while tokens:
        changed = False
        while tokens and tokens[-1] == "(":
            tokens.pop()
            changed = True
        if tokens and tokens[-1] == "await":
            tokens.pop()
            changed = True
        if not changed:
            break
    if len(tokens) >= 2 and tokens[-2:] == ["&", "&"]:
        return not _literal_tokens(tokens[:-2], "true")
    if len(tokens) >= 2 and tokens[-2:] == ["|", "|"]:
        return not _literal_tokens(tokens[:-2], "false")
    return False


def _exec_detail(method: str, text: str) -> str:
    if method == "apply_patch":
        names = (os.path.basename(name.strip())
                 for name in _PATCH_FILE.findall(_js_unescape(text)))
        return ", ".join(dict.fromkeys(names))
    if method == "update_plan":
        step = _PLAN_STEP.search(text)
        if step:
            return _js_unescape(step.group(1) or step.group(2))
        return _js_string(text, "step")
    for key in _EXEC_KEYS:
        value = _js_string(text, key)
        if value:
            return value
    return ""


def _display_call(name: str, detail: str) -> tuple[str, str]:
    if name not in _SHELL_TOOLS:
        return name, detail
    calls = shell.commands(detail)
    if not calls:
        return "shell", ""
    program, arguments = calls[0]
    return program, " ".join(arguments)


def _unwrap_exec(value) -> tuple[
        str, str, str, tuple[tuple[str, str, str], ...]] | None:
    """Codex's `exec` tool wraps the real call in a JS snippet. Recover the inner
    display call plus every call's complete recognition body."""
    if not isinstance(value, str):
        return None
    decoded = _js_unescape(value)
    calls = []
    for call in _INNER_CALL.finditer(value):
        scan = _scan_js(value, call.start(), collect_statement=True)
        if (scan is None or scan.brace_depth != 0
                or _js_call_is_skipped(scan.statement)):
            continue
        args = _call_args(value, call.end())
        if args is None:
            continue
        method = call.group(1)
        shell = method in ("exec_command", "shell", "bash")
        detail = (_shell_command(value, args, call.start()) if shell
                  else _exec_detail(method, value))
        body = detail if shell and detail else decoded
        recognition_body = detail if shell else body[:BODY_CLIP]
        calls.append((method, one_line(detail), body[:BODY_CLIP], recognition_body))
    if not calls:
        return None
    method, detail, body, _ = calls[0]
    recognized = tuple((name, item_detail, recognition)
                       for name, item_detail, _, recognition in calls)
    return method, detail, body, recognized


def _date_dirs(root: str, session_id: str = "") -> set[str]:
    dates = {datetime.now(), datetime.now(timezone.utc)}
    compact = session_id.replace("-", "")
    if len(compact) == 32 and compact[12:13].lower() == "7":
        try:
            timestamp = int(compact[:12], 16) / 1000
            dates.update({
                datetime.fromtimestamp(timestamp),
                datetime.fromtimestamp(timestamp, timezone.utc),
            })
        except (OSError, OverflowError, ValueError):
            pass
    return {os.path.join(root, f"{now.year:04d}", f"{now.month:02d}", f"{now.day:02d}")
            for now in dates}


def _codex_relevant(name: str, kind: str) -> bool:
    return kind == "directory" or name.endswith(".jsonl")


def _codex_admit(
    context: object,
    _path: str,
    record: object,
    required: dict[str, object],
) -> bool | None:
    if (
        type(context) is not tuple
        or len(context) != 2
        or type(context[0]) is not str
        or type(context[1]) is not str
    ):
        return None
    if not isinstance(record, AdmissionProof) or record.provider != "codex":
        return None
    requested_path, group_id = context
    if group_id:
        return record.root_identity == (group_id,)
    requested = required.get(requested_path)
    if not isinstance(requested, AdmissionProof) or requested.provider != "codex":
        return None
    return record.root_identity == requested.root_identity


def _codex_text(value: object) -> bool:
    if type(value) is str:
        return True
    if type(value) is list:
        return all(_codex_text(item) for item in value)
    if type(value) is not dict:
        return False
    if any(
        key in value and value[key] is not None and type(value[key]) is not str
        for key in ("text", "output_text", "input_text", "message")
    ):
        return False
    return all(
        key not in value or value[key] is None or _codex_text(value[key])
        for key in ("content", "output", "summary")
    )


def _subagent_activity(payload: dict) -> dict | None:
    if payload.get("type") == "sub_agent_activity":
        return payload
    if payload.get("type") != "item_completed":
        return None
    item = payload.get("item")
    if type(item) is not dict or item.get("type") != "SubAgentActivity":
        return None
    return {
        "type": "sub_agent_activity",
        "agent_thread_id": item.get("agent_thread_id"),
        "agent_path": item.get("agent_path"),
        "event_id": item.get("id"),
        "kind": item.get("kind"),
    }


def _validate_codex_record(record: object) -> None:
    if (
        type(record) is not dict
        or type(record.get("payload")) is not dict
        or not _finite_json_value(record)
    ):
        raise ValueError("invalid Codex replacement record")
    if not _optional_type(record, "timestamp", str):
        raise ValueError("invalid Codex record timestamp")
    top = record.get("type")
    if top is not None and type(top) is not str:
        raise ValueError("invalid Codex record type")
    payload = record["payload"]
    kind = payload.get("type")
    if kind is not None and type(kind) is not str:
        raise ValueError("invalid Codex payload type")
    if top == "session_meta":
        if not _valid_codex_meta(payload):
            raise ValueError("invalid Codex session metadata")
        return
    if top == "turn_context":
        if not _optional_type(payload, "model", str):
            raise ValueError("invalid Codex model")
        return
    if top == "inter_agent_communication_metadata":
        if (
            "trigger_turn" in payload
            and type(payload["trigger_turn"]) is not bool
        ):
            raise ValueError("invalid Codex trigger marker")
        return
    if top == "event_msg":
        activity = _subagent_activity(payload)
        if activity is not None:
            if not all(
                _optional_type(activity, field, str)
                for field in ("agent_thread_id", "agent_path", "event_id", "kind")
            ):
                raise ValueError("invalid Codex sub-agent activity")
            return
        if kind == "token_count":
            info = payload.get("info", {})
            total = info.get("total_token_usage", {}) if type(info) is dict else None
            if type(info) is not dict or type(total) is not dict or not _optional_type(
                total, "output_tokens", int,
            ):
                raise ValueError("invalid Codex token count")
        elif kind == "agent_message":
            if type(payload.get("message")) is not str or not _optional_exact_type(
                payload, "phase", str,
            ):
                raise ValueError("invalid Codex agent message")
        elif kind == "user_message":
            values = [payload[key] for key in ("message", "content", "text") if key in payload]
            if any(value is not None and not _codex_text(value) for value in values):
                raise ValueError("invalid Codex user message")
        elif kind in ("task_started", "task_complete", "turn_aborted"):
            if not _optional_type(payload, "turn_id", str):
                raise ValueError("invalid Codex turn id")
            for field in ("last_agent_message", "reason"):
                if field in payload and payload[field] is not None and not _codex_text(payload[field]):
                    raise ValueError("invalid Codex lifecycle text")
        return
    if top != "response_item":
        return
    if kind in ("function_call", "custom_tool_call"):
        if not all(
            _optional_exact_type(payload, field, str)
            for field in ("call_id", "id", "name", "status")
        ) or (
            kind == "function_call"
            and not _optional_exact_type(payload, "arguments", str)
        ) or (
            kind == "custom_tool_call"
            and not _optional_exact_type(payload, "input", str)
        ):
            raise ValueError("invalid Codex tool call")
        inputs, _ = _call_input(payload)
        if type(inputs) is dict:
            validate_recognizer_inputs(payload.get("name") or "", inputs)
    elif kind in ("function_call_output", "custom_tool_call_output"):
        if not all(
            _optional_exact_type(payload, field, str)
            for field in ("call_id", "status")
        ) or (
            "output" in payload
            and payload["output"] is not None
            and not _codex_text(payload["output"])
        ):
            raise ValueError("invalid Codex tool output")
    elif kind == "reasoning":
        for field in ("summary", "content"):
            if field in payload and payload[field] is not None and not _codex_text(payload[field]):
                raise ValueError("invalid Codex reasoning")
    elif kind == "message":
        if not _optional_exact_type(payload, "role", str):
            raise ValueError("invalid Codex message role")
        if payload.get("role") == "assistant" and (
            not _optional_exact_type(payload, "phase", str)
            or (
                "content" in payload
                and not _codex_text(payload["content"])
            )
        ):
            raise ValueError("invalid Codex assistant message")
    elif kind == "agent_message":
        if not all(
            _optional_exact_type(payload, field, str)
            for field in ("author", "recipient")
        ) or (
            "content" in payload
            and not _codex_text(payload["content"])
        ):
            raise ValueError("invalid Codex inter-agent message")


@dataclass
class _FileState:
    path: str
    meta: dict
    thread_id: str
    node_id: str
    ready: bool
    offset: int = 0
    carry: bytes = b""
    source_signature: tuple[int, ...] | None = None
    saw_trigger: bool = False
    pending_started: tuple[dict, float] | None = None
    pending_context: dict | None = None
    turn_id: str = ""
    last_text: str = ""
    last_text_phase: str = ""
    last_text_origin: str = ""
    open_dispatch: str = ""
    open_spawns: list[Step] = field(default_factory=list)
    dispatch_count: int = 0



@dataclass(frozen=True)
class _PreparedCodexAppend:
    action: str
    cohort: CapturedCohort
    metadata: tuple[tuple[str, dict], ...]
    pending: tuple[tuple[str, tuple[int, ...]], ...]
    root: tuple[str, dict, str, str, bool] | None = None


class CodexModel:
    provider = "codex"

    def __init__(self, path: str):
        requested = os.path.realpath(path)
        self._initialize(
            requested,
            sessions_root(requested),
            {},
            requested,
            {},
            "",
            "",
            False,
        )

    def _initialize(
        self,
        requested_path: str,
        sessions_root: str,
        metadata: dict[str, dict],
        root_path: str,
        root_meta: dict,
        group_id: str,
        root_thread: str,
        root_resolved: bool,
    ) -> None:
        self._requested_path = requested_path
        self._sessions = sessions_root
        self._pending_candidates: dict[str, tuple[int, ...]] = {}
        self._metadata = metadata
        self.path = root_path
        self._root_meta = root_meta
        self._group_id = group_id
        self._root_thread = root_thread
        self._root_resolved = root_resolved
        self.nodes: dict[str, Node] = {
            "main": Node(id="main", label="Codex main", parent=None,
                         status="running", kind="main")
        }
        self.task_list: dict = {}
        self._states: dict[str, _FileState] = {}
        self._thread_nodes = {self._root_thread: "main"}
        self._steps: dict[str, Step] = {}
        self._pending_outputs: dict[str, tuple[str, str]] = {}
        self._spawn_steps: dict[str, Step] = {}
        self._spawn_threads: dict[str, str] = {}
        self._dispatch_spawns: dict[str, list[Step]] = {}
        self._known_paths: set[str] = set()
        self._irrelevant_paths: dict[str, tuple[int, ...]] = {}
        self._excluded_threads: set[str] = set()
        self._directory_stamps: dict[str, DirectoryStamp] = {}
        self._initial_changed = True
        self._replaying = False
        self._admit_group(self._metadata)

    def reload_plan(self):
        return ReloadPlan(
            (self._requested_path,),
            tuple(
                DirectoryRule(
                    directory,
                    _codex_relevant,
                    recursive=False,
                    admit=_codex_admit,
                )
                for directory in sorted(self._candidate_dirs())
            ),
            admission_context=(self._requested_path, self._group_id),
            admission_provider="codex",
        )

    @staticmethod
    def _captured_meta(cohort: CapturedCohort, source) -> dict:
        admission = cohort.admission_for(source.path)
        if (
            admission is None
            or admission.proof is None
            or admission.proof.provider != "codex"
        ):
            return {}
        payload = admission.record.get("payload")
        if not _valid_codex_meta(payload):
            raise ValueError("invalid Codex source identity")
        clean = {
            name: payload[name]
            for name in (
                "id", "session_id", "cwd", "thread_source",
                "parent_thread_id", "forked_from_id", "agent_path",
                "agent_nickname",
            )
            if name in payload
        }
        source_value = payload.get("source")
        if type(source_value) is str or source_value is None:
            if "source" in payload:
                clean["source"] = source_value
            return clean
        clean_source = {}
        if "subagent" in source_value:
            subagent = source_value["subagent"]
            clean_subagent = {}
            if "other" in subagent:
                clean_subagent["other"] = True
            if "thread_spawn" in subagent:
                spawn = subagent["thread_spawn"]
                clean_subagent["thread_spawn"] = (
                    None
                    if spawn is None
                    else {
                        name: spawn[name]
                        for name in (
                            "parent_thread_id", "depth", "agent_path",
                            "agent_nickname",
                        )
                        if name in spawn
                    }
                )
            clean_source["subagent"] = clean_subagent
        clean["source"] = clean_source
        return clean

    def prepare_append(self, cohort: CapturedCohort) -> _PreparedCodexAppend:
        metadata: dict[str, dict] = {}
        pending = {
            path: _stamp_tuple(stamp)
            for path, stamp in cohort.denied
            if cohort.admission_failure_for(path) in {"empty", "pending"}
        }
        for source in cohort.files:
            meta = self._captured_meta(cohort, source)
            if meta:
                metadata[source.path] = meta
            elif source.path not in self._known_paths:
                pending[source.path] = source.signature
        root = None
        if cohort.action == "bootstrap":
            requested_meta = metadata.get(self._requested_path)
            if requested_meta is None and self._requested_path != self.path:
                requested_meta = metadata.get(self.path)
            if requested_meta is None:
                raise ValueError("missing Codex requested source identity")
            group_id = requested_meta.get("session_id") or requested_meta["id"]
            root_path = next((
                path
                for path, meta in metadata.items()
                if (meta.get("session_id") or meta.get("id")) == group_id
                and meta.get("id") == group_id
                and not _internal_subagent(meta)
            ), "")
            root_resolved = bool(root_path)
            if not root_path and _internal_subagent(requested_meta):
                raise ValueError("internal Codex subagent rollout is not monitorable")
            root_path = root_path or self._requested_path
            root_meta = metadata.get(root_path)
            if root_meta is None:
                raise ValueError("missing Codex root source identity")
            root = (
                root_path,
                root_meta,
                root_meta.get("session_id") or group_id,
                root_meta.get("id") or group_id,
                root_resolved,
            )
        for source in cohort.files:
            for record in cohort.records_for(source.path):
                if type(record) is not dict or type(record.get("payload")) is not dict:
                    continue
                _validate_codex_record(record)
        return _PreparedCodexAppend(
            cohort.action,
            cohort,
            tuple(sorted(metadata.items())),
            tuple(sorted(pending.items())),
            root,
        )

    def apply_append(self, prepared: object) -> bool:
        batch = prepared
        if type(batch) is not _PreparedCodexAppend:
            raise TypeError("invalid prepared Codex append")
        cohort = batch.cohort
        metadata = dict(batch.metadata)
        prior_nodes = self.nodes
        prior_tasks = self.task_list
        if batch.action == "bootstrap":
            root_path, root_meta, group_id, root_thread, root_resolved = batch.root
            self._initialize(
                self._requested_path,
                self._sessions,
                metadata,
                root_path,
                root_meta,
                group_id,
                root_thread,
                root_resolved,
            )
            self._pending_candidates = dict(batch.pending)
            changed = True
        else:
            changed = False
            self._pending_candidates.update(batch.pending)
            for source in cohort.files:
                if source.path in self._states:
                    continue
                meta = metadata.get(source.path)
                if not meta:
                    continue
                self._pending_candidates.pop(source.path, None)
                self._irrelevant_paths.pop(source.path, None)
                self._metadata[source.path] = meta
                if (meta.get("session_id") or meta.get("id")) == self._group_id:
                    changed |= self._admit_group({source.path: meta})
                else:
                    self._irrelevant_paths[source.path] = source.signature
        captured = {source.path: source for source in cohort.files}
        for state in sorted(
            (
                state for state in self._states.values()
                if state.path in captured
            ),
            key=lambda item: (item.node_id != "main", item.path),
        ):
            source = captured[state.path]
            records = cohort.records_for(state.path)
            if state.offset == 0 and not state.ready:
                has_gate = any(
                    type(record) is dict
                    and record.get("type") == "inter_agent_communication_metadata"
                    and type(record.get("payload")) is dict
                    and record["payload"].get("trigger_turn")
                    for record in records
                )
                has_copied_session = any(
                    type(record) is dict
                    and record.get("type") == "session_meta"
                    and type(record.get("payload")) is dict
                    and record["payload"].get("id") != state.thread_id
                    for record in records
                )
                if not has_gate and not has_copied_session:
                    state.ready = True
            for record in records:
                if type(record) is not dict or type(record.get("payload")) is not dict:
                    continue
                changed |= self._ingest(state, record)
            state.offset = source.offset
            state.carry = b""
            state.source_signature = source.signature
        self._pending_candidates = {
            path: signature
            for path, signature in self._pending_candidates.items()
            if path not in self._states
            and path in dict(cohort.admission_records)
        }
        self._initial_changed = False
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
        return False

    def _admit_group(self, metadata: dict[str, dict]) -> bool:
        changed = False
        if not self._root_resolved:
            root = next((
                (path, meta)
                for path, meta in metadata.items()
                if meta.get("id") == self._group_id
                and (meta.get("session_id") or meta.get("id")) == self._group_id
                and not _internal_subagent(meta)
            ), None)
            if root is not None:
                changed |= self._promote_root(*root)
        for path, meta in metadata.items():
            session_id = meta.get("session_id") or meta.get("id")
            if session_id != self._group_id or path in self._known_paths:
                continue
            if _internal_subagent(meta):
                changed |= self._exclude_thread(meta.get("id") or "")
                continue
            changed |= self._admit(path, meta)
        for state in self._states.values():
            changed |= self._apply_parent(state)
        return changed

    def _promote_root(self, path: str, meta: dict) -> bool:
        prior_thread = self._root_thread
        if not prior_thread or prior_thread == self._group_id:
            self.path = path
            self._root_meta = meta
            self._root_thread = self._group_id
            self._root_resolved = True
            self._thread_nodes[self._group_id] = "main"
            return True

        child_id = "cx:" + prior_thread
        prior_main = self.nodes["main"]
        prior_meta = self._root_meta
        agent_path = prior_meta.get("agent_path") or ""
        prior_main.id = child_id
        prior_main.label = one_line(agent_path.rsplit("/", 1)[-1]) or "agent"
        prior_main.detail = str(prior_meta.get("agent_nickname") or "")[:DETAIL_CLIP]
        prior_main.parent = "main"
        prior_main.kind = "agent"
        for node_id in prior_main.children:
            child = self.nodes.get(node_id)
            if child is not None and child.parent == "main" and child.kind == "dispatch":
                child.parent = child_id
        self.nodes[child_id] = prior_main
        self.nodes["main"] = Node(
            id="main", label="Codex main", parent=None,
            status="running", kind="main",
        )
        for state in self._states.values():
            if state.node_id == "main":
                state.node_id = child_id
        self._thread_nodes[prior_thread] = child_id
        self._thread_nodes[self._group_id] = "main"
        self.path = path
        self._root_meta = meta
        self._root_thread = self._group_id
        self._root_resolved = True
        return True

    def _admit(self, path: str, meta: dict) -> bool:
        thread_id = meta.get("id")
        if not thread_id:
            return False
        is_root = thread_id == self._root_thread
        node_id = "main" if is_root else "cx:" + thread_id
        if not is_root:
            detail = meta.get("agent_nickname") or ""
            if node_id not in self.nodes:
                self.nodes[node_id] = Node(id=node_id, label="agent",
                                           detail=detail[:DETAIL_CLIP],
                                           parent="main", status="running", kind="agent")
                self.nodes["main"].children.append(node_id)
            else:
                self.nodes[node_id].detail = detail[:DETAIL_CLIP]
        self._thread_nodes[thread_id] = node_id
        source = meta.get("source")
        subagent = (meta.get("thread_source") == "subagent" or
                    isinstance(source, dict) and isinstance(source.get("subagent"), dict))
        self._states[path] = _FileState(path, meta, thread_id, node_id, not subagent)
        self._known_paths.add(path)
        return True

    def _exclude_thread(self, thread_id: str) -> bool:
        if not thread_id:
            return False
        self._excluded_threads.add(thread_id)
        node_id = self._thread_nodes.pop(thread_id, "")
        node = self.nodes.get(node_id)
        if not node or node_id == "main":
            return False
        if node.parent in self.nodes and node_id in self.nodes[node.parent].children:
            self.nodes[node.parent].children.remove(node_id)
        self.nodes.pop(node_id, None)
        return True

    def _apply_parent(self, state: _FileState) -> bool:
        if state.node_id == "main":
            return False
        parent_thread = state.meta.get("parent_thread_id")
        source = state.meta.get("source")
        if not parent_thread and isinstance(source, dict):
            subagent = source.get("subagent")
            spawn = subagent.get("thread_spawn") if isinstance(subagent, dict) else None
            if isinstance(spawn, dict):
                parent_thread = spawn.get("parent_thread_id")
        parent_id = self._thread_nodes.get(parent_thread, "main")
        spawn = self._spawn_steps.get(state.thread_id)
        if spawn and spawn.dispatch in self.nodes:
            parent_id = spawn.dispatch
        node = self.nodes[state.node_id]
        if node.parent == parent_id or parent_id == state.node_id:
            return False
        if node.parent in self.nodes and state.node_id in self.nodes[node.parent].children:
            self.nodes[node.parent].children.remove(state.node_id)
        node.parent = parent_id
        if state.node_id not in self.nodes[parent_id].children:
            self.nodes[parent_id].children.append(state.node_id)
        return True

    def _candidate_dirs(self) -> set[str]:
        directories = {os.path.dirname(path) for path in self._known_paths}
        directories.add(os.path.dirname(self.path))
        directories.update(_date_dirs(self._sessions, self._group_id))
        return directories

    def poll(self) -> bool:
        return poll_append(self)

    @property
    def source_revision(self) -> int:
        return _source_revision(self)

    @property
    def visible_revision(self) -> int:
        return _visible_revision(self)

    def observation_paths(self) -> set[str]:
        paths = set(self._known_paths)
        paths.update(self._candidate_dirs())
        return paths

    def accepts_observation(self, paths) -> bool:
        return _reload_accepts_observation(self, paths)

    def lifecycle_identity(self) -> tuple[str, str] | None:
        if self._group_id:
            return "codex", self._group_id
        digest = hashlib.sha256(self._requested_path.encode("utf-8")).hexdigest()[:32]
        return "codex", "pending-" + digest

    def apply_lifecycle(self, fact: LifecycleFact, target: str, status: str,
                        preserve_error: bool = False) -> bool:
        created = False
        if target == "main":
            node = self.nodes["main"]
        else:
            node_id = self._thread_nodes.get(target, "cx:" + target)
            node = self.nodes.get(node_id)
            if node is None:
                label = fact.agent_type
                if not label or label == "default":
                    label = "agent"
                node = Node(
                    id=node_id,
                    label=label,
                    parent="main",
                    status="running",
                    kind="agent",
                )
                self.nodes[node_id] = node
                self.nodes["main"].children.append(node_id)
                self._thread_nodes[target] = node_id
                created = True
        if preserve_error and node.status == "error":
            return False
        changed = created or node.status != status
        node.status = status
        result = fact.result[:RESULT_CLIP]
        if result and status in {"done", "error"} and node.result != result:
            node.result = result
            changed = True
        if changed:
            _advance_visible_revision(self)
        return changed

    def _ingest(self, state: _FileState, record: dict) -> bool:
        if not isinstance(record, dict):
            return False
        top = record.get("type")
        payload = record.get("payload")
        if not isinstance(payload, dict):
            return False
        ts = parse_timestamp(record.get("timestamp"))
        if not state.ready:
            if (not self._root_resolved and state.node_id == "main"
                    and top == "response_item"
                    and payload.get("type") in ("function_call", "custom_tool_call")):
                return self._response(state, payload, ts)
            if top == "event_msg" and payload.get("type") == "task_started":
                state.pending_started = (payload, ts)
            elif top == "turn_context":
                state.pending_context = payload
            elif top == "inter_agent_communication_metadata" and payload.get("trigger_turn"):
                state.saw_trigger = True
            elif (top == "response_item" and payload.get("type") == "agent_message" and
                  state.saw_trigger and payload.get("recipient") == state.meta.get("agent_path")):
                state.ready = True
                changed = self._apply_context(state, state.pending_context or {})
                if state.pending_started:
                    pending, pending_ts = state.pending_started
                    changed |= self._lifecycle(state, pending, pending_ts)
                return changed
            return False
        if top == "turn_context":
            return self._apply_context(state, payload)
        if top == "event_msg":
            return self._event(state, payload, ts)
        if top == "response_item":
            return self._response(state, payload, ts)
        return False

    def _apply_context(self, state: _FileState, payload: dict) -> bool:
        model_name = payload.get("model") or ""
        node = self.nodes[state.node_id]
        if not model_name or node.model == model_name:
            return False
        node.model = model_name
        return True

    def _event(self, state: _FileState, payload: dict, ts: float) -> bool:
        event_type = payload.get("type")
        activity = _subagent_activity(payload)
        if activity is not None:
            return self._activity(state, activity, ts)
        if event_type in ("task_started", "task_complete", "turn_aborted"):
            return self._lifecycle(state, payload, ts)
        if event_type == "user_message":
            text = _plain_text(
                payload.get("message") or payload.get("content") or payload.get("text")
            ).strip()
            if not text:
                return False
            self._close_dispatch(state)
            node = self.nodes[state.node_id]
            node.steps.append(Step(
                "prompt", "task: " + one_line(text)[:DETAIL_CLIP], text[:BODY_CLIP], ts,
            ))
            node.last_ts = max(node.last_ts, ts)
            return True
        if event_type == "token_count":
            usage = payload.get("info") or {}
            total = usage.get("total_token_usage") or {}
            tokens = int(total.get("output_tokens", 0) or 0)
            node = self.nodes[state.node_id]
            if node.tokens == tokens:
                return False
            node.tokens = tokens
            return True
        if event_type == "agent_message":
            text = _plain_text(payload.get("message")).strip()
            changed = self._add_text(state, text, payload.get("phase") or "", ts, "event")
            if text and payload.get("phase") == "final_answer":
                self.nodes[state.node_id].result = text[:RESULT_CLIP]
                changed = True
            return changed
        return False

    def _lifecycle(self, state: _FileState, payload: dict, ts: float) -> bool:
        self._close_dispatch(state)
        node = self.nodes[state.node_id]
        event_type = payload.get("type")
        turn_id = payload.get("turn_id") or ""
        if event_type == "task_started":
            state.turn_id = turn_id
            changed = node.status != "running" or bool(node.result)
            node.status = "running"
            node.result = ""
        elif state.turn_id and turn_id and turn_id != state.turn_id:
            return False
        elif event_type == "task_complete":
            changed = node.status != "done"
            node.status = "done"
            result = _plain_text(payload.get("last_agent_message")).strip()
            if result:
                node.result = result[:RESULT_CLIP]
                changed = True
        else:
            changed = node.status != "error"
            node.status = "error"
            reason = _plain_text(payload.get("reason")).strip()
            if reason:
                node.result = reason[:RESULT_CLIP]
                changed = True
        node.last_ts = max(node.last_ts, ts)
        self._sync_spawn(state.thread_id, node.status)
        return changed

    def _response(self, state: _FileState, payload: dict, ts: float) -> bool:
        payload_type = payload.get("type")
        if payload_type in ("function_call", "custom_tool_call"):
            return self._record_call(state, payload, ts)
        if payload_type in ("function_call_output", "custom_tool_call_output"):
            return self._record_output(payload)
        if payload_type == "reasoning":
            self._close_dispatch(state)
            text = _plain_text(payload.get("summary") or payload.get("content")).strip()
            if not text:
                return False
            node = self.nodes[state.node_id]
            node.steps.append(Step("thinking", "thinking: " + one_line(text),
                                   text[:BODY_CLIP], ts))
            node.last_ts = max(node.last_ts, ts)
            return True
        if payload_type == "message" and payload.get("role") == "assistant":
            text = _plain_text(payload.get("content")).strip()
            return self._add_text(state, text, payload.get("phase") or "", ts, "response")
        return False

    def _add_text(self, state: _FileState, text: str, phase: str, ts: float,
                  origin: str) -> bool:
        if not text:
            return False
        self._close_dispatch(state)
        mirrored = (text == state.last_text and phase == state.last_text_phase and
                    origin != state.last_text_origin)
        if mirrored:
            state.last_text = ""
            state.last_text_phase = ""
            state.last_text_origin = ""
            return False
        state.last_text = text
        state.last_text_phase = phase
        state.last_text_origin = origin
        node = self.nodes[state.node_id]
        node.steps.append(Step("text", "text: " + one_line(text), text[:BODY_CLIP], ts))
        node.last_ts = max(node.last_ts, ts)
        if phase == "final_answer":
            node.result = text[:RESULT_CLIP]
        return True

    def _record_call(self, state: _FileState, payload: dict, ts: float) -> bool:
        call_id = payload.get("call_id") or payload.get("id") or ""
        name = payload.get("name") or "tool"
        value, raw = _call_input(payload)
        task_name = ""
        unwrapped = _unwrap_exec(value)
        if unwrapped:
            name, detail, body, inner_calls = unwrapped
            recognized = tuple(workflow.codex_call(*inner) for inner in inner_calls)
        else:
            task_name = value.get("task_name") if isinstance(value, dict) else ""
            if isinstance(value, str):
                detail = one_line(_exec_detail(name, value))
            else:
                detail = task_name or self._call_detail(value)
            body = _body_for(value, raw)
            recognized = (workflow.codex_bare_call(name, value),)
        for call in recognized:
            self._annotate(self.nodes[state.node_id], *call)
        spawn = name == "spawn_agent"
        if not spawn:
            self._close_dispatch(state)
        display_name, display_detail = _display_call(name, detail)
        title = clip_text(
            f"{display_name}: {display_detail}" if display_detail else display_name,
            78,
        )
        spawn_name = one_line(task_name) if isinstance(task_name, str) else ""
        step = Step("spawn" if spawn else "tool", title, body, ts,
                    status="running", tid=call_id,
                    goal=spawn_name if spawn else "")
        if spawn:
            self._join_dispatch(state, step, call_id)
        self.nodes[state.node_id].steps.append(step)
        self.nodes[state.node_id].last_ts = max(self.nodes[state.node_id].last_ts, ts)
        if call_id:
            self._steps[call_id] = step
            if spawn:
                self._spawn_steps[call_id] = step
            pending = self._pending_outputs.pop(call_id, None)
            if pending:
                self._apply_output(step, *pending)
        status = str(payload.get("status") or "").lower()
        if status in FAILED:
            step.status = "error"
        return True

    @staticmethod
    def _close_dispatch(state: _FileState) -> None:
        state.open_dispatch = ""
        state.open_spawns.clear()

    def _join_dispatch(self, state: _FileState, step: Step, call_id: str) -> None:
        if not state.open_dispatch:
            state.dispatch_count += 1
            key = call_id or str(len(self.nodes[state.node_id].steps) + 1)
            state.open_dispatch = f"dispatch:{state.node_id}:{key}"
            state.open_spawns = []
        step.dispatch = state.open_dispatch
        state.open_spawns.append(step)
        self._dispatch_spawns.setdefault(state.open_dispatch, []).append(step)
        if len(state.open_spawns) == 2:
            batch = Node(
                id=state.open_dispatch,
                label=batch_label((spawn.goal for spawn in state.open_spawns), state.dispatch_count),
                parent=state.node_id,
                status="running",
                kind="dispatch",
            )
            self.nodes[batch.id] = batch
            parent = self.nodes[state.node_id]
            positions = [
                parent.children.index(spawn.child)
                for spawn in state.open_spawns
                if spawn.child in parent.children
            ]
            position = min(positions) if positions else len(parent.children)
            parent.children.insert(position, batch.id)
            for spawn in state.open_spawns:
                self._place_in_dispatch(spawn)
        elif state.open_dispatch in self.nodes:
            self.nodes[state.open_dispatch].label = batch_label(
                (spawn.goal for spawn in state.open_spawns), state.dispatch_count)

    def _place_in_dispatch(self, step: Step) -> None:
        if not step.child or step.dispatch not in self.nodes:
            return
        batch = self.nodes[step.dispatch]
        child = self.nodes.get(step.child)
        if not child:
            return
        if child.parent in self.nodes and child.id in self.nodes[child.parent].children:
            self.nodes[child.parent].children.remove(child.id)
        child.parent = batch.id
        if child.id not in batch.children:
            batch.children.append(child.id)
        spawn_order = [
            spawn.child
            for spawn in self._dispatch_spawns.get(batch.id, [])
            if spawn.child in self.nodes
        ]
        batch.children[:] = spawn_order + [
            child_id for child_id in batch.children if child_id not in spawn_order
        ]

    @staticmethod
    def _annotate(node: Node, name: str, inp: dict) -> None:
        """Record skill and workflow signals from one recognized call."""
        skill = skill_mode.skill_of(name, inp)
        if skill and (not node.skills or node.skills[-1] != skill):
            node.skills.append(skill)
        workflow.note(node, name, inp, skill)

    @staticmethod
    def _call_detail(value) -> str:
        if not isinstance(value, dict):
            return ""
        for key in ("cmd", "command", "description", "file_path", "path", "pattern", "url"):
            item = value.get(key)
            if isinstance(item, str) and item:
                return one_line(item)
        return ""

    def _record_output(self, payload: dict) -> bool:
        call_id = payload.get("call_id") or ""
        text = _plain_text(payload.get("output")).strip()
        status = str(payload.get("status") or "done").lower()
        status = "error" if status in FAILED else "done"
        step = self._steps.get(call_id)
        if not step:
            self._pending_outputs[call_id] = (text, status)
            return False
        self._apply_output(step, text, status)
        return True

    def _apply_output(self, step: Step, text: str, status: str) -> None:
        if text:
            step.output = text[:BODY_CLIP]
        if step.kind != "spawn" or status == "error":
            step.status = status

    def _activity(self, state: _FileState, payload: dict, ts: float) -> bool:
        thread_id = payload.get("agent_thread_id") or ""
        if not thread_id or thread_id in self._excluded_threads:
            return False
        event_id = payload.get("event_id") or ""
        kind = payload.get("kind") or ""
        step = self._spawn_steps.get(event_id)
        node_id = self._thread_nodes.get(thread_id)
        if not node_id:
            node_id = "cx:" + thread_id
            label = step.goal if step and step.goal else "agent"
            self.nodes[node_id] = Node(id=node_id, label=label, parent=state.node_id,
                                       status="running", kind="agent")
            self.nodes[state.node_id].children.append(node_id)
            self._thread_nodes[thread_id] = node_id
        if kind == "started" and step:
            step.child = node_id
            self._spawn_threads[event_id] = thread_id
            self._spawn_steps[thread_id] = step
            node = self.nodes[node_id]
            if step.goal:
                node.label = step.goal
            if step.dispatch not in self.nodes and node.parent == state.node_id:
                parent = self.nodes[state.node_id]
                if node_id in parent.children:
                    parent.children.remove(node_id)
                parent.children.append(node_id)
            self._place_in_dispatch(step)
        node = self.nodes[node_id]
        node.last_ts = max(node.last_ts, ts)
        if kind in FAILED:
            node.status = "error"
            if step:
                step.status = "error"
        return True

    def _sync_spawn(self, thread_id: str, status: str) -> None:
        step = self._spawn_steps.get(thread_id)
        if step and status in ("done", "error"):
            step.status = status
