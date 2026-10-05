"""Transcript-derived skill invocations from recorded tool calls."""
from __future__ import annotations

import re

from . import shell


_BASH_TOOLS = {"Bash", "bash", "exec_command"}
_SKILL_FILE = re.compile(
    r"(?:^|/)([^/]+)/SKILL\.md\Z|(?:^|/)skills/([^/]+)\.md\Z")
_SKILL_IN_COMMAND = re.compile(r"([^/\s'\"]+)/SKILL\.md\b")
_READ_TOOLS = {"Read", "read", "view"}


def skill_of(name: str, inp: dict) -> str:
    """Return skill loaded by tool call, or empty string."""
    if name == "Skill":
        return str(inp.get("skill") or "").strip()
    if name in _READ_TOOLS:
        found = _SKILL_FILE.search(str(inp.get("path") or inp.get("file_path") or ""))
        return (found.group(1) or found.group(2)).strip() if found else ""
    if name in _BASH_TOOLS:
        for program, arguments in shell.commands(shell.command_of(inp)):
            if not shell.reads(program, arguments):
                continue
            found = _SKILL_IN_COMMAND.search(" ".join(arguments))
            if found:
                return found.group(1).strip()
    return ""
