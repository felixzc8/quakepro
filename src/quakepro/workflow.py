"""Transcript-derived orchestrate-workflow annotations: which workflow skill an
agent loaded, what it did to tickets and boards, which reviews it dispatched, and
the phase those signals imply. Pure recognizers over tool_use name/input, like
skill_mode — no project files are read and no CLI is run.
"""
from __future__ import annotations

import re

from . import shell

# Workflow skills of the orchestrate set and the phase each one implies.
PHASE_OF_SKILL = {
    "orchestrate": "orchestrating",
    "to-spec": "spec",
    "to-tickets": "tickets",
    "tdd": "implement",
    "implement": "implement",
    "code-review": "review",
    "review-falsification": "review",
}
KEEP_EVENTS = 20   # per node, newest last

# Ticket files the workflow writes: .scratch/<feature>/issues/<NN>-<slug>.md
_TICKET = re.compile(r"(?:^|/)\.scratch/[^/]+/issues/(\d+-[^/]+)\.md\Z")
_STATUS = re.compile(r"\*\*Status:\*\*[ \t]*([^\n*]+)")
# The board subcommands worth surfacing.
_BOARD_SUBS = {"show", "read", "open", "stop", "list"}
# A blackboard checkout's own CLI entry point, run through node and friends.
_BOARD_CLI = re.compile(r"blackboard[^\s]*/dist/cli\.js\Z")
# Dispatching the falsification review names the skill or its title line; a prompt
# that merely mentions a coming review does not.
_REVIEW = re.compile(r"review-falsification|review\s+by\s+falsification", re.I)
# Codex writes files as patch hunks rather than through a write tool.
_PATCH_FILE = re.compile(r"^\*\*\* (Add|Update|Delete) File: (.+)$", re.M)
# Inside one file's hunks, a status the patch adds and a status it removes.
_PATCH_ADDED = re.compile(r"^\+[ \t]*\*\*Status:\*\*[ \t]*([^\n*]+)", re.M)
_PATCH_REMOVED = re.compile(r"^-[ \t]*\*\*Status:\*\*[ \t]*([^\n*]+)", re.M)

_WRITE_TOOLS = {"Write", "write"}
_EDIT_TOOLS = {"Edit", "MultiEdit", "NotebookEdit", "edit"}
_BASH_TOOLS = {"Bash", "bash", "exec_command"}
_SPAWN_TOOLS = {"Task", "Agent", "subagent", "spawn_agent"}
# Codex encrypts its spawn message, so that dispatch cannot be classified.
_OPAQUE_SPAWN_TOOLS = {"spawn_agent"}
_PATH_KEYS = ("file_path", "notebook_path", "path")
_TEXT_KEYS = ("new_string", "newText", "new_str", "content", "text")
_OLD_TEXT_KEYS = ("old_string", "oldText", "old_str")
_PROMPT_KEYS = ("prompt", "message", "task", "description", "instructions", "task_name")


def _path_of(inp: dict) -> str:
    for key in _PATH_KEYS:
        value = inp.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _text_of(inp: dict, keys) -> str:
    """One side of an edit or write, across the providers' argument names,
    including MultiEdit's list of replacements."""
    parts = [str(inp.get(key)) for key in keys if isinstance(inp.get(key), str)]
    edits = inp.get("edits")
    if isinstance(edits, list):
        for edit in edits:
            if isinstance(edit, dict):
                parts.append(_text_of(edit, keys))
    return "\n".join(parts)


def _status_of(text: str) -> str:
    found = _STATUS.search(text)
    return found.group(1).strip() if found else ""


def ticket_event(name: str, inp: dict) -> str:
    """"ticket <id> created" or "ticket <id> → <status>" for a tool call that wrote a
    workflow ticket file or moved its Status line, else ""."""
    if name == "apply_patch":
        return _patch_ticket_event(str(inp.get("patch") or ""))
    ticket = _TICKET.search(_path_of(inp))
    if not ticket:
        return ""
    if name in _WRITE_TOOLS:
        return "ticket %s created" % ticket.group(1)
    if name in _EDIT_TOOLS:
        status = _status_of(_text_of(inp, _TEXT_KEYS))
        if not status or status == _status_of(_text_of(inp, _OLD_TEXT_KEYS)):
            return ""
        return "ticket %s → %s" % (ticket.group(1), status)
    return ""


def _patch_files(patch: str):
    """Each file the patch touches, as (verb, path, that file's hunks). A file's
    status lines belong to that file only."""
    marks = list(_PATCH_FILE.finditer(patch))
    for index, mark in enumerate(marks):
        end = marks[index + 1].start() if index + 1 < len(marks) else len(patch)
        yield mark.group(1), mark.group(2).strip(), patch[mark.end():end]


def _patch_ticket_event(patch: str) -> str:
    for verb, path, hunks in _patch_files(patch):
        ticket = _TICKET.search(path)
        if not ticket:
            continue
        if verb == "Add":
            return "ticket %s created" % ticket.group(1)
        added = _PATCH_ADDED.search(hunks)
        if not added:
            continue
        removed = _PATCH_REMOVED.search(hunks)
        status = added.group(1).strip()
        if removed and removed.group(1).strip() == status:
            continue
        return "ticket %s → %s" % (ticket.group(1), status)
    return ""


def board_event(name: str, inp: dict) -> str:
    """"board <subcommand>" for a shell call driving a Blackboard board, else "".
    The board program has to be what the shell runs, not a word inside an argument."""
    if name not in _BASH_TOOLS:
        return ""
    for program, arguments in shell.commands(shell.command_of(inp)):
        words = list(arguments)
        if program != "blackboard":
            while words and not _BOARD_CLI.search(words[0]):
                words.pop(0)
            if not words:
                continue
            words.pop(0)
        if words and words[0] in _BOARD_SUBS:
            return "board " + words[0]
    return ""


def review_event(name: str, inp: dict) -> str:
    """"review dispatched" when a subagent launch dispatches the falsification
    review, "agent dispatched" when the launch text cannot be read, else ""."""
    if name not in _SPAWN_TOOLS:
        return ""
    text = " ".join(str(inp.get(key) or "") for key in _PROMPT_KEYS)
    if _REVIEW.search(text):
        return "review dispatched"
    return "agent dispatched" if name in _OPAQUE_SPAWN_TOOLS else ""


def phase_of_skill(skill: str) -> str:
    """The phase a loaded skill implies."""
    return PHASE_OF_SKILL.get(skill, "")


def _phase_of_event(event: str) -> str:
    """Only a review dispatch names a phase. Ticket and board traffic happens in
    every phase — a ticket status change is the first thing implementing does — so
    those stay events (decision phase-from-latest-signal)."""
    return "review" if event == "review dispatched" else ""


def note(node, name: str, inp: dict, skill: str = "") -> None:
    """Record on `node` whatever workflow signal one tool call carries: its ticket,
    board, or review event, and the phase the latest signal implies."""
    event = ticket_event(name, inp) or board_event(name, inp) or review_event(name, inp)
    if event:
        node.workflow.append(event)
        del node.workflow[:-KEEP_EVENTS]
    phase = phase_of_skill(skill) or _phase_of_event(event)
    if phase:
        node.phase = phase


def codex_call(method: str, detail: str, body: str) -> tuple:
    """Normalize one Codex call — the inner tool name, one-line detail and body its
    unified `exec` wrapper carries — into the (tool name, input) shape the
    recognizers here and in skill_mode read."""
    if method == "apply_patch":
        return "apply_patch", {"patch": body}
    if method == "spawn_agent":
        return "spawn_agent", {"prompt": body}
    if method in ("exec_command", "shell", "bash"):
        return "bash", {"command": body or detail}
    if method in ("read_file", "read", "view"):
        return "read", {"path": detail}
    if method in ("write_file", "write", "create_file"):
        return "write", {"path": detail, "content": body}
    return method, {}


def codex_bare_call(name: str, value) -> tuple:
    """Normalize a Codex call made without the exec wrapper: an argument object as
    given, or apply_patch's raw patch text, into the (tool name, input) shape the
    recognizers read."""
    if isinstance(value, dict):
        return name, value
    if name == "apply_patch" and isinstance(value, str):
        return name, {"patch": value}
    return name, {}
