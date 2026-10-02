"""Shell command line reading for the transcript recognizers: which programs a
command line runs, and with which arguments. Enough to tell a program name from a
word that merely appears inside an argument.
"""
from __future__ import annotations

import re

_SPLIT = re.compile(r"&&|\|\||[;|\n&]")
_ENV = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
_REDIRECT = re.compile(r"[<>]")

# Commands that read a file out to the transcript, as opposed to searching,
# writing, or deleting it.
READERS = {"cat", "sed", "head", "tail", "nl", "bat", "less", "more", "view"}


def command_of(inp: dict) -> str:
    """The shell command a tool call carries, across the providers' argument names."""
    return str(inp.get("command") or inp.get("cmd") or "")


def commands(line: str) -> list[tuple[str, list[str]]]:
    """One (program, arguments) pair per command the line runs. The program is the
    bare name, so `/usr/bin/sed` and `sed` read alike."""
    found = []
    for part in _SPLIT.split(line):
        words = part.split()
        while words and _ENV.match(words[0]):
            words.pop(0)
        if words:
            found.append((words[0].rsplit("/", 1)[-1], words[1:]))
    return found


def reads(program: str, arguments: list[str]) -> bool:
    """Whether this command reads its arguments out rather than changing them. A
    redirection makes it a write however it starts, e.g. `cat > file <<'EOF'`."""
    if program not in READERS:
        return False
    return not any(_REDIRECT.search(word) for word in arguments)
