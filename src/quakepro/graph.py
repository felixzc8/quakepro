"""Render the model as a clean vertical, indented tree.

Every agent is a node connected to its parent by curved box-drawing branches
(├─ for a middle child, ╰─ for the last, │ to carry the line down). A colored
icon shows stable state (✓ done, ▶ running, … waiting, ✗ errored).
Role badge and label color show role. Structural containers place a kind badge
after their status icon (⚙ workflow, ⧉ agent team, ⌂ run root,
◆ agent batch); dispatch rows use a rounded gray outline.

The graph shows only the agent hierarchy — a node's own work (its thoughts, spoken
turns, tool calls and spawns) is not drawn inline; drilling into an agent opens a
bottom panel that holds its own action graph (`render_actions`) — one step
per row, with each step's exact input/output hidden until it is expanded.

Returns a Rich Text, its pixel width, the DFS draw order, and a `view` map
(drawn id -> ViewNode with parent/children/depth) for cursor navigation.
"""
from __future__ import annotations

from dataclasses import dataclass
from rich.cells import cell_len, set_cell_size, split_graphemes
from rich.console import Console
from rich.text import Text

from .agent_roles import (
    GENERIC_AGENT_NAMES,
    ROLE_ICONS,
    classify_roles,
    role_icon,
    role_style,
)
from .session_model import Node, SessionModel, Step, Task

BRANCH = "bright_black"
ACTION_DETAIL = "white"
HEADER = "bold yellow"   # reserved for section headers (instructions / actions)
MAXNAME = 38
RUNNING_GLYPH = "▶"
_TREE_GUTTER = 2
# Type labels too generic to identify a node — fall back to its task line instead.
GENERIC = set(GENERIC_AGENT_NAMES)
# Depth -> colour. ANSI names so we ride the user's terminal palette; cycles past the end.
LEVEL_STYLES = ["bold white", "cyan", "green", "magenta", "bright_blue", "bright_yellow"]
# Shared lifecycle colors for tree, drill, and timeline status cues.
STATUS_STYLES = {
    "running": "yellow",
    "waiting": "bright_black",
    "done": "green",
    "error": "red",
}
GROUP_STYLE = "bold cyan"
DISPATCH_STYLE = "grey82"
# Per-step-kind glyphs; running and error states can override tool/spawn glyphs.
STEP_GLYPH = {"prompt": "·", "thinking": "✶", "text": "▪", "tool": "▸", "spawn": "◆"}
# Badge drawn after the status icon to mark a structural container by kind.
NODE_BADGE = {"workflow": "⚙", "team": "⧉", "root": "⌂", "dispatch": "◆"}
# Shared-task-list nodes: a checkbox glyph + colour per status.
TASK_GLYPH = {"pending": "▢", "in_progress": "▨", "completed": "▣"}
TASK_COLOR = {"pending": "bright_black", "in_progress": "white", "completed": "green"}

_WRAP_CONSOLE = Console()
_REPLACEMENT = "�"
_INLINE_LAYOUT = {"\t", "\n", "\r", "\v", "\f", "\x85", "\u2028", "\u2029"}


def _safe_characters(value):
    """Yield Unicode scalars, repairing malformed surrogate input."""
    text = "" if value is None else value if isinstance(value, str) else str(value)
    i = 0
    while i < len(text):
        code = ord(text[i])
        if 0xD800 <= code <= 0xDBFF and i + 1 < len(text):
            low = ord(text[i + 1])
            if 0xDC00 <= low <= 0xDFFF:
                yield chr(0x10000 + ((code - 0xD800) << 10) + low - 0xDC00)
                i += 2
                continue
        yield _REPLACEMENT if 0xD800 <= code <= 0xDFFF else text[i]
        i += 1


def sanitize_text(value) -> str:
    """Return display text with terminal control characters made inert.

    Newlines and tabs remain intact so transcript formatting is preserved. Other
    C0 controls, DEL, and C1 controls are replaced rather than sent to the
    terminal, which also neutralizes ANSI and OSC escape sequences.
    """
    return "".join(
        ch if ch in "\n\t" or not (ord(ch) < 32 or 127 <= ord(ch) <= 159)
        else _REPLACEMENT
        for ch in _safe_characters(value)
    )


def sanitize_inline_text(value) -> str:
    """Return safe single-line display text.

    Layout characters become ordinary spaces so model fields cannot add rows or
    invalidate navigation coordinates. Other terminal controls remain visible as
    replacement characters.
    """
    return "".join(
        " " if ch in _INLINE_LAYOUT else
        ch if not (ord(ch) < 32 or 127 <= ord(ch) <= 159) else _REPLACEMENT
        for ch in _safe_characters(value)
    )


def task_style(status: str) -> tuple[str, str]:
    return TASK_GLYPH.get(status, "▢"), TASK_COLOR.get(status, "bright_black")


class ViewNode:
    """A drawn row's place in the tree, so navigation never has to re-walk."""
    __slots__ = ("id", "parent", "children", "depth", "row", "x", "w")

    def __init__(self, vid: str, parent: str | None, depth: int,
                 row: int = 0, x: int = 0, w: int = 0) -> None:
        self.id = vid
        self.parent = parent
        self.children: list[str] = []
        self.depth = depth
        self.row = row
        self.x = x          # column where this row's glyph starts
        self.w = w          # drawn width of glyph + label, for scroll-into-view


def level_style(depth: int) -> str:
    return LEVEL_STYLES[depth % len(LEVEL_STYLES)]


def status_glyph(status: str) -> str:
    """Return one stable glyph for each lifecycle state."""
    if status == "done":
        return "✓"
    if status == "error":
        return "✗"
    if status == "waiting":
        return "…"
    return RUNNING_GLYPH


def status_icon_style(status: str) -> str:
    return status_style(status)


def status_style(status: str) -> str:
    return STATUS_STYLES.get(status, STATUS_STYLES["running"])


def step_glyph_style(st: Step, depth: int) -> tuple[str, str]:
    """Return stable glyph and colour for an action."""
    col = level_style(depth)
    if st.kind in ("tool", "spawn"):
        if st.status == "error":
            return "✗", "red"
        if st.status == "running":
            return RUNNING_GLYPH, col
        return STEP_GLYPH[st.kind], col
    if st.kind == "thinking":
        return STEP_GLYPH["thinking"], "bright_black"
    return STEP_GLYPH.get(st.kind, "·"), col


def _clip(s: str, n: int = MAXNAME) -> str:
    s = sanitize_inline_text(s).strip()
    if n <= 0:
        return ""
    if cell_len(s) <= n:
        return s
    ellipsis = "…"
    budget = n - cell_len(ellipsis)
    spans, _ = split_graphemes(s)
    end = used = i = 0
    while i < len(spans):
        start, span_end, cells = spans[i]
        first = ord(s[start])
        if 0x1F1E6 <= first <= 0x1F1FF and i + 1 < len(spans):
            next_start, next_end, next_cells = spans[i + 1]
            if 0x1F1E6 <= ord(s[next_start]) <= 0x1F1FF:
                span_end, cells, i = next_end, cells + next_cells, i + 1
        if used + cells > budget:
            break
        used += cells
        end = span_end
        i += 1
    return s[:end].rstrip() + ellipsis


def _cell_graphemes(value: str):
    spans, _ = split_graphemes(value)
    index = 0
    while index < len(spans):
        start, end, cells = spans[index]
        first = ord(value[start])
        if 0x1F1E6 <= first <= 0x1F1FF and index + 1 < len(spans):
            next_start, next_end, next_cells = spans[index + 1]
            if 0x1F1E6 <= ord(value[next_start]) <= 0x1F1FF:
                end = next_end
                cells += next_cells
                index += 1
        yield value[start:end], cells
        index += 1


def wrap_text(text: str, width: int) -> list[str]:
    """Wrap safe multiline text to a terminal-cell width."""
    width = max(width, 1)
    out: list[str] = []
    for ln in sanitize_text(text).splitlines() or [""]:
        lines = Text(ln).wrap(_WRAP_CONSOLE, width, overflow="fold")
        out.extend(line.plain for line in lines or [Text()])
    return out


def _wrap(text: str, w: int) -> list[str]:
    return wrap_text(text, w)


def _short(node: Node, idx: int) -> str:
    """Best available short label for a node line: its type when specific, else its
    task/result line — more telling than a bare 'general-purpose' across siblings."""
    lbl = sanitize_inline_text(node.label).replace("⚙ ", "").strip()
    if lbl == "main loop":
        return "main"
    if lbl and lbl.lower() not in GENERIC:
        return _clip(lbl)
    d = sanitize_inline_text(node.detail).strip()
    if d:
        return _clip(d)
    r = sanitize_inline_text(node.result).strip()
    if r:
        return _clip(r)
    return f"a{idx}"


def _dispatch_label(nodes, node) -> str:
    counts = {status: 0 for status in ("running", "waiting", "done", "error")}
    for child_id in node.children:
        child = nodes.get(child_id)
        if child:
            counts[child.status if child.status in counts else "running"] += 1
    total = sum(counts.values())
    noun = "agent" if total == 1 else "agents"
    states = " · ".join(
        f"{count} {status}"
        for status, count in counts.items()
        if count
    )
    title = _clip(node.label)
    prefix = f"{title} · " if title else ""
    return f"{prefix}{total} {noun}" + (f" · {states}" if states else "")


def skill_tag(node) -> str:
    """Return latest recorded skill as a dim tree annotation."""
    skills = getattr(node, "skills", None)
    return "  ⟡ " + _clip(sanitize_inline_text(skills[-1])) if skills else ""


def workflow_tag(node) -> str:
    """Dim per-node orchestrate-workflow annotation: the phase the node's latest
    workflow signal implies, then its latest ticket, board, or review event. Empty
    for nodes with no workflow activity."""
    parts = []
    phase = getattr(node, "phase", "")
    if phase:
        parts.append("▷ " + _clip(sanitize_inline_text(phase)))
    events = getattr(node, "workflow", None)
    if events:
        parts.append("⊞ " + _clip(sanitize_inline_text(events[-1])))
    return ("  " + "  ".join(parts)) if parts else ""


def _role_legend(present_roles: set[str]) -> Text:
    if not present_roles:
        return Text()
    legend = Text("roles ", ACTION_DETAIL)
    first = True
    for role, icon in ROLE_ICONS.items():
        if role not in present_roles:
            continue
        if not first:
            legend.append(" · ", ACTION_DETAIL)
        legend.append(f"{icon} {role}", role_style(role))
        first = False
    return legend


def render_tree(
    model: SessionModel,
    root_id: str,
    stop_kinds=(),
    selected=None,
    collapsed=(),
    skills=False,
    available_width: int | None = None,
):
    nodes = model.nodes
    if root_id not in nodes:
        return Text("(nothing running yet)", "bright_black"), 24, [], {}

    out = Text()
    order: list[str] = []
    view: dict[str, ViewNode] = {}
    width = [0]
    roles = classify_roles(nodes)

    def emit(vid: str, parent: str | None, prefix: str, connector: str,
             depth: int, glyph: str, glyph_style: str, label_style: str,
             badge: str | None, badge_style: str, label: str, hidden: int,
             etag: str = "", outlined: bool = False) -> None:
        x = cell_len(prefix) + cell_len(connector)
        tag = etag + (f"  +{hidden}" if hidden else "")
        outline_left = "╭─ " if outlined else ""
        outline_right = " ─╮" if outlined else ""
        row_width = (
            cell_len(glyph) + 1 + cell_len(outline_left)
            + (cell_len(badge) + 1 if badge else 0)
            + cell_len(label) + cell_len(outline_right) + cell_len(tag)
        )
        view[vid] = ViewNode(vid, parent, depth, len(order), x, row_width)
        if parent is not None and parent in view:
            view[parent].children.append(vid)
        order.append(vid)
        if connector:
            out.append(prefix + connector, BRANCH)
        out.append(glyph + " ", glyph_style)
        if outline_left:
            out.append(outline_left, DISPATCH_STYLE)
        if badge:
            out.append(badge + " ", badge_style)
        selected_style = (
            f"reverse {label_style}"
            if "bold" in label_style.split()
            else f"reverse bold {label_style}"
        )
        out.append(label, selected_style if vid == selected else label_style)
        if outline_right:
            out.append(outline_right, DISPATCH_STYLE)
        if tag:
            out.append(tag, ACTION_DETAIL)
        out.append("\n")
        width[0] = max(width[0], x + row_width)

    def emit_node(nid: str, parent: str | None, prefix: str, connector: str,
                  last: bool, depth: int) -> None:
        n = nodes.get(nid)
        if not n:
            return
        if n.kind == "task":
            g, glyph_style = task_style(n.status)
            label_style = "white"
            badge = None
            badge_style = glyph_style
        else:
            g = status_glyph(n.status)
            role = roles.get(nid)
            badge = (
                role_icon(role) if role
                else NODE_BADGE.get(n.kind)
            )
            glyph_style = status_icon_style(n.status)
            grouping = n.kind in NODE_BADGE
            label_style = (
                DISPATCH_STYLE if n.kind == "dispatch"
                else GROUP_STYLE if grouping
                else role_style(role) if role
                else "white"
            )
            badge_style = (
                DISPATCH_STYLE if n.kind == "dispatch"
                else GROUP_STYLE if grouping
                else label_style
            )
        kids = [] if n.kind in stop_kinds else list(n.children)
        shut = bool(kids) and nid in collapsed   # collapsed node -> children hidden, show count
        label = _dispatch_label(nodes, n) if n.kind == "dispatch" else _short(n, len(order) + 1)
        emit(nid, parent, prefix, connector, depth, g, glyph_style, label_style,
             badge, badge_style, label, len(kids) if shut else 0,
             workflow_tag(n) + (skill_tag(n) if skills else ""),
             outlined=n.kind == "dispatch")
        if shut:
            return
        child_prefix = (prefix + ("   " if last else "│  ")) if connector else ""
        for i, k in enumerate(kids):
            klast = i == len(kids) - 1
            emit_node(k, nid, child_prefix, "╰─ " if klast else "├─ ", klast, depth + 1)

    emit_node(root_id, None, "", "", True, 0)
    legend = _role_legend({roles[nid] for nid in order if nid in roles})
    if legend:
        legend_width = (
            max(int(available_width) - _TREE_GUTTER, 1)
            if available_width is not None
            else max(cell_len(legend.plain), 1)
        )
        legend_lines = legend.wrap(
            _WRAP_CONSOLE, legend_width, overflow="fold",
        )
        body = out
        out = Text()
        for line in legend_lines:
            out.append(line)
            out.append("\n")
        out.append_text(body)
        for node in view.values():
            node.row += len(legend_lines)
        width[0] = max(
            width[0],
            max((cell_len(line.plain) for line in legend_lines), default=0),
        )
    return out, max(width[0] + _TREE_GUTTER, 12), order, view


_TIMELINE_CUE = {
    "parallel": "║",
    "active": "▶",
    "failed": "✗",
    "missing-time": "?",
}
_PARALLEL_BLUE_START = (175, 215, 255)
_PARALLEL_BLUE_END = (95, 95, 255)


def _elapsed(seconds: float) -> str:
    seconds = max(int(seconds), 0)
    minutes, second = divmod(seconds, 60)
    hours, minute = divmod(minutes, 60)
    if hours:
        return f"{hours}h{minute:02d}m"
    if minutes:
        return f"{minutes}m{second:02d}s"
    return f"{second}s"


def _timeline_status(timeline) -> str:
    statuses = {lane.status for lane in timeline.lanes}
    return next(
        (status for status in ("error", "running", "waiting", "done")
         if status in statuses),
        timeline.status,
    )


def _cue_marks(lane) -> str:
    return "".join(_TIMELINE_CUE[cue] for cue in lane.cues)


def _timeline_position(ts: float, start: float, end: float, width: int) -> int:
    if width <= 1 or end <= start:
        return 0
    return round((ts - start) * (width - 1) / (end - start))


def _draw_lane(lane, start: float, end: float, width: int) -> tuple[str, int | None]:
    cells = [" "] * width
    status_position = None
    fill = {
        "done": "━",
        "running": "═",
        "waiting": "┄",
        "error": "─",
    }
    terminal = {
        "done": "●",
        "running": "▶",
        "waiting": "○",
        "error": "✗",
    }
    if lane.started_at > 0 and lane.latest_at > 0:
        left = _timeline_position(lane.started_at, start, end, width)
        right = _timeline_position(lane.latest_at, start, end, width)
        if right > left:
            for index in range(left, right + 1):
                cells[index] = fill.get(lane.status, "┄")
        cells[right] = terminal.get(lane.status, "○")
        status_position = right

    if "missing-time" in lane.cues:
        cells[0] = "?"
        status_position = 0
    return "".join(cells), status_position


def _run_line_style(lane, parallel_section_count: int) -> str:
    if lane.parallel_section is None:
        return BRANCH
    progress = (
        lane.parallel_section / (parallel_section_count - 1)
        if parallel_section_count > 1 else 0
    )
    red, green, blue = (
        round(start + (end - start) * progress)
        for start, end in zip(_PARALLEL_BLUE_START, _PARALLEL_BLUE_END)
    )
    return f"#{red:02x}{green:02x}{blue:02x}"


_TIMELINE_HELP = {
    "parallel": "parallel",
    "active": "active",
    "failed": "failed",
    "missing-time": "untimed",
}


def _timeline_help(timeline, compact: bool = False) -> str:
    present = {
        cue
        for lane in timeline.lanes
        for cue in lane.cues
    }
    separator = " · " if compact else " "
    return "cues " + separator.join(
        (
            f"{_TIMELINE_CUE[cue]}:{_TIMELINE_HELP[cue]}"
            if compact else f"{_TIMELINE_CUE[cue]} {_TIMELINE_HELP[cue]}"
        )
        for cue in _TIMELINE_CUE
        if cue in present
    )


def _compact_timeline_rows(index: int, lane, width: int) -> list[tuple[str, str]]:
    identity = f"{index + 1} {role_icon(lane.role)} {sanitize_inline_text(lane.label)}"
    marks = _cue_marks(lane) or "│"
    return (
        [(line, role_style(lane.role)) for line in wrap_text(identity, width)]
        + [(line, status_style(lane.status)) for line in wrap_text(marks, width)]
    )


def render_timeline(timeline, width: int):
    """Render recorded agent-run time on one scale within explicit cell width."""
    available = max(int(width), 1)
    out = Text()
    count = len(timeline.lanes)
    parallel_section_count = 1 + max(
        (
            lane.parallel_section for lane in timeline.lanes
            if lane.parallel_section is not None
        ),
        default=-1,
    )
    if available < 20:
        header = _clip(f"T {count}", available)
        out.append(header + "\n", HEADER)
        drawn = cell_len(header)
        for index, lane in enumerate(timeline.lanes):
            for line, style in _compact_timeline_rows(index, lane, available):
                out.append(line + "\n", style)
                drawn = max(drawn, cell_len(line))
        return out, drawn

    header = _clip(
        f"Timeline · {count} "
        f"{'agent' if count == 1 else 'agents'} · "
        f"{_timeline_status(timeline)}",
        available,
    )
    out.append(header + "\n", HEADER)
    drawn = cell_len(header)
    help_line = _timeline_help(timeline, compact=available < 64)
    if help_line != "cues ":
        for line in wrap_text(help_line, available):
            out.append(line + "\n", BRANCH)
            drawn = max(drawn, cell_len(line))
    present_roles = {lane.role for lane in timeline.lanes}
    role_help = _role_legend(present_roles)
    if role_help:
        for line in role_help.wrap(_WRAP_CONSOLE, available, overflow="fold"):
            out.append(line)
            out.append("\n")
            drawn = max(drawn, cell_len(line.plain))

    if available < 64:
        for index, lane in enumerate(timeline.lanes):
            marks = _cue_marks(lane) or "│"
            roles = role_icon(lane.role)
            label = sanitize_inline_text(lane.label)
            if lane.started_at <= 0 or lane.latest_at <= 0:
                span = "?"
            elif lane.started_at == lane.latest_at:
                span = _elapsed(lane.started_at - timeline.started_at)
            else:
                span = (
                    _elapsed(lane.started_at - timeline.started_at)
                    + "→"
                    + _elapsed(lane.latest_at - timeline.started_at)
                )
            prefix = f"{index + 1:02d} "
            identity = f"{roles} {label}"
            suffix = f" · {span} · {lane.status}"
            plain = _clip(prefix + marks + " " + identity + suffix, available)
            line = Text(plain, BRANCH)
            marks_start = len(prefix)
            marks_end = min(marks_start + len(marks), len(plain))
            identity_start = marks_start + len(marks) + 1
            identity_end = min(identity_start + len(identity), len(plain))
            line.stylize(status_style(lane.status), marks_start, marks_end)
            line.stylize(role_style(lane.role), identity_start, identity_end)
            out.append(line)
            out.append("\n")
            drawn = max(drawn, cell_len(plain))
        return out, drawn

    role_width = min(max(
        (cell_len(role_icon(lane.role)) for lane in timeline.lanes),
        default=0,
    ), 8)
    cue_width = 7
    label_width = min(24, max(14, available // 4))
    chart_width = max(
        12, available - role_width - cue_width - label_width - 3,
    )
    start_label = "0s"
    end_label = _elapsed(timeline.latest_at - timeline.started_at)
    scale = start_label + " " * max(
        chart_width - cell_len(start_label) - cell_len(end_label), 1) + end_label
    scale = set_cell_size(scale, chart_width)
    scale_line = " " * (role_width + cue_width + label_width + 3) + scale
    out.append(scale_line + "\n", BRANCH)
    drawn = max(drawn, cell_len(scale_line))

    for lane in timeline.lanes:
        roles = set_cell_size(
            _clip(
                role_icon(lane.role),
                role_width,
            ),
            role_width,
        )
        marks = set_cell_size(_cue_marks(lane), cue_width)
        label = set_cell_size(
            _clip(sanitize_inline_text(lane.label), label_width),
            label_width,
        )
        chart, status_position = _draw_lane(
            lane,
            timeline.started_at,
            timeline.latest_at,
            chart_width,
        )
        out.append(roles, role_style(lane.role))
        out.append(" ")
        out.append(marks, status_style(lane.status))
        out.append(" ")
        out.append(label, role_style(lane.role))
        out.append(" ")
        chart_text = Text(chart, _run_line_style(lane, parallel_section_count))
        if status_position is not None:
            chart_text.stylize(
                status_style(lane.status),
                status_position,
                status_position + 1,
            )
        out.append_text(chart_text)
        out.append("\n")
        line = f"{roles} {marks} {label} {chart}"
        drawn = max(drawn, cell_len(line))
    return out, min(drawn, available)


def render_task_board(out, tasks: list[Task]) -> int:
    """Append the team's shared task list as a board under the tree: one row per
    task — status glyph, #id + subject, then '→ owner' (the teammate that claimed
    it, or — when unclaimed). Returns the board's drawn width."""
    out.append("\ntasks\n", HEADER)
    labels = ["#%s %s" % (sanitize_inline_text(t.id), sanitize_inline_text(t.subject))
              for t in tasks]
    lw = min(max((cell_len(_clip(s)) for s in labels), default=0), MAXNAME)
    width = 0
    for t, lbl in zip(tasks, labels):
        g, col = task_style(t.status)
        clipped = _clip(lbl)
        owner = sanitize_inline_text(t.owner) or "—"
        status = sanitize_inline_text(t.status)
        out.append(g + " ", col)
        out.append(set_cell_size(clipped, lw), "white")
        out.append("  → ", BRANCH)
        out.append(owner, "cyan" if t.owner else "bright_black")
        out.append("  " + status, "bright_black")
        width = max(width, 2 + lw + 4 + cell_len(owner) + 2 + cell_len(status))
        out.append("\n")
    return width


def render_actions(out, steps: list[Step], selected, expanded, depth: int,
                   rows_out=None, width: int = 0) -> tuple[int, int]:
    """Draw one agent's actions as a vertical graph in the drill panel: each step
    (thought, spoken turn, tool call, spawn) is a circle-node joined like the agent
    tree. Titles only — a step's exact body and output stay hidden until it is in
    `expanded` (the panel reveals them when you drill in on it). Returns the first
    and last rows of the selected step (title through its expanded body) so the
    caller can both scroll it into view and page through a long body. If rows_out is
    given, each step's (start row, index) is appended for click-to-select mapping."""
    out.append("actions\n", HEADER)
    rows, sel_row, sel_end, n = 1, 0, 0, len(steps)
    for i, st in enumerate(steps):
        last = i == n - 1
        start = rows
        if rows_out is not None:
            rows_out.append((start, i))
        g, glyph_style = step_glyph_style(st, depth)
        out.append("╰─ " if last else "├─ ", BRANCH)
        out.append(g + " ", glyph_style)
        title = sanitize_inline_text(st.title)
        status = sanitize_inline_text(st.status)
        klabel, sep, info = title.partition(": ")
        klabel += sep
        label_style = "bright_black" if st.kind == "thinking" else level_style(depth)
        if i == selected:
            out.append(title, "reverse bold")
        else:
            out.append(klabel, label_style)
            out.append(info, ACTION_DETAIL)
        if st.kind in ("tool", "spawn"):
            out.append("  " + status, "bright_black")
        out.append("\n")
        rows += 1
        if i in expanded:
            cont = ("   " if last else "│  ") + "  "
            if st.body:
                if st.kind == "spawn":
                    out.append(cont, BRANCH)
                    out.append("— instructions —\n", "bold")
                    rows += 1
                # Spoken-turn prose has no newlines of its own — hard-wrap it to the
                # panel so a long reply reads as paragraphs instead of running off-screen.
                if st.kind == "text" and width:
                    lines = wrap_text(st.body, width - cell_len(cont))
                else:
                    lines = sanitize_text(st.body).splitlines()
                for ln in lines:
                    out.append(cont, BRANCH)
                    out.append(ln + "\n", "bright_black")
                    rows += 1
            if st.output:
                out.append(cont, BRANCH)
                out.append("— output —\n", "bold")
                rows += 1
                for ln in sanitize_text(st.output).splitlines():
                    out.append(cont, BRANCH)
                    out.append(ln + "\n", "bright_black")
                    rows += 1
        if i == selected:
            sel_row, sel_end = start, rows - 1
    return sel_row, sel_end
