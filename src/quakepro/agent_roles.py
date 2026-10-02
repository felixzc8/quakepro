"""Provider-neutral subagent role classification from visible agent names."""
from __future__ import annotations

import re

from .session_model import Node, one_line


ROLE_ICONS = {
    "research": "⌕",
    "design": "◇",
    "code": "⌨",
    "tests": "⚗",
    "review": "◎",
    "debug": "⚠",
    "docs": "≡",
    "coordinate": "◆",
    "other": "·",
}
ROLE_STYLES = {
    "research": "bright_blue",
    "design": "medium_turquoise",
    "code": "pale_turquoise1",
    "tests": "orchid1",
    "review": "bright_yellow",
    "debug": "bright_red",
    "docs": "bright_white",
    "coordinate": "bright_magenta",
    "other": "light_slate_blue",
}

_ROLE_PATTERNS = (
    ("code", r"\b(?:(?:hot|bug)fix(?:er|ing|ed|es)?|fix(?:er|ing|ed|es)?|repair(?:er|ing|ed|s)?|correct(?:or|ion|ions|ing|ed|s)?|resolv(?:e|er|ing|ed|es))\b"),
    ("review", r"\b(?:review(?:er|ing|ed|s|\d+)?|rereview(?:er|ing|ed|s|\d+)?|audit(?:or|ing|ed|s)?|critique(?:r|ing|ed|s)?|conformance|quality\s+blind(?:\s+r\d+)?|falsif(?:y|ier|ying|ied|ies|ication))\b"),
    ("debug", r"\b(?:debug(?:ger|ging|ged|s)?|diagnos(?:e|er|ing|ed|is|tic|tics)|triag(?:e|er|ing|ed))\b|\broot[ -]?cause\b|\breproduc(?:e|ing|ed)\b"),
    ("tests", r"\b(?:qa|benchmark|tests?|tester|testing|verif(?:y|ier|ying|ied|ies|ication)|validat(?:e|or|ing|ed|es|ion)|check(?:er|ing|ed|s)?)\b"),
    ("docs", r"\b(?:docs?|documentation|readme|changelog)\b"),
    ("design", r"\b(?:design(?:er|ing|ed|s)?|spec(?:ification|writer|ing|ed|s)?|plan(?:ner|ning|ned|s)?|architect(?:ure|ural|ing|ed|s)?)\b"),
    ("research", r"\b(?:research(?:er|ing|ed|es)?|explor(?:e|er|ing|ed)|investigat(?:e|or|ing|ed)|analy[sz](?:e|er|ing|ed)|gather(?:er|ing|ed|s)?)\b"),
    ("coordinate", r"\b(?:coordinat(?:e|or|ing|ed|es)|orchestrat(?:e|or|ing|ed|es)|dispatch(?:er|ing|ed|es)?|delegat(?:e|or|ing|ed|es)|synthesi[sz](?:e|er|ing|ed)|supervisor|owner)\b"),
    ("code", r"\b(?:cod(?:e|er|ing|ed|es)|impl|implement(?:ation|er|ing|ed|s)?|build(?:er|ing|s)?|built|dev|develop(?:er|ing|ed|s)?|refactor(?:er|ing|ed|s)?)\b"),
)
GENERIC_AGENT_NAMES = frozenset({
    "agent", "claude", "general-purpose", "subagent", "teammate",
})


def _role_in(name: str) -> str:
    words = re.sub(r"[^a-z0-9]+", " ", name.casefold())
    return next(
        (role for role, pattern in _ROLE_PATTERNS if re.search(pattern, words)),
        "",
    )


def role_from_name(label: str, detail: str = "") -> str:
    """Return first role keyword found in visible agent name."""
    name = one_line(label)
    if name.casefold() in GENERIC_AGENT_NAMES and one_line(detail):
        name = one_line(detail)
    return _role_in(name) or "other"


def classify_role(node: Node) -> str:
    return role_from_name(node.label, node.detail)


def classify_roles(nodes: dict[str, Node]) -> dict[str, str]:
    """Classify every agent node from its visible name."""
    return {
        node_id: classify_role(node)
        for node_id, node in nodes.items()
        if node.kind in {"agent", "teammate"}
    }


def role_icon(role: str) -> str:
    return ROLE_ICONS.get(role, ROLE_ICONS["other"])


def role_style(role: str) -> str:
    return ROLE_STYLES.get(role, ROLE_STYLES["other"])
