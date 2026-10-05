"""Tests for graph.py's pure rendering helpers and the DFS tree walk."""
import quakepro.graph as graph
from rich.cells import cell_len
from rich.console import Console
from rich.text import Text

from quakepro.graph import (level_style, sanitize_text, sanitize_inline_text, status_glyph,
                   status_icon_style, status_style,
                   step_glyph_style, task_style, _short, _clip, _wrap, wrap_text, render_tree,
                   render_actions, render_task_board, RUNNING_GLYPH, LEVEL_STYLES, MAXNAME)
from quakepro.claude_model import ClaudeModel
from quakepro.session_model import Node, Step, Task


def _assert_terminal_safe(value: str) -> None:
    assert all(ch in "\n\t" or not (ord(ch) < 32 or 127 <= ord(ch) <= 159)
               for ch in value)
    value.encode("utf-8")


def test_status_glyph_states():
    assert status_glyph("done") == "✓"
    assert status_glyph("error") == "✗"
    assert status_glyph("waiting") == "…"
    assert status_glyph("running") == RUNNING_GLYPH
    statuses = ("done", "error", "waiting", "running")
    assert len({status_glyph(status) for status in statuses}) == 4
    assert all(cell_len(status_glyph(status)) == 1 for status in statuses)


def test_agent_status_icon_style_uses_lifecycle_colors():
    assert status_icon_style("running") == "yellow"
    assert status_icon_style("waiting") == "bright_black"
    assert status_icon_style("done") == "green"
    assert status_icon_style("error") == "red"
    assert status_icon_style("unexpected") == "yellow"


def test_status_style_states_and_fallback():
    assert status_style("running") == "yellow"
    assert status_style("waiting") == "bright_black"
    assert status_style("done") == "green"
    assert status_style("error") == "red"
    assert status_style("unexpected") == "yellow"


def test_step_glyph_states():
    running = Step("tool", "Bash", status="running")
    assert step_glyph_style(running, 1) == (RUNNING_GLYPH, LEVEL_STYLES[1])
    assert step_glyph_style(Step("tool", "Bash", status="error"), 1) == ("✗", "red")
    assert step_glyph_style(Step("spawn", "Agent"), 1)[0] == "◆"
    assert step_glyph_style(Step("thinking", "thinking"), 1) == ("✶", "bright_black")


def test_task_style_states_and_fallback():
    assert task_style("pending") == ("▢", "bright_black")
    assert task_style("in_progress") == ("▨", "white")
    assert task_style("completed") == ("▣", "green")
    assert task_style("unexpected") == ("▢", "bright_black")


def test_level_style_cycles_by_depth():
    assert level_style(0) == LEVEL_STYLES[0]
    assert level_style(len(LEVEL_STYLES)) == LEVEL_STYLES[0]  # wraps around


def test_graph_palette_smoke_renders_with_ansi_terminal():
    console = Console(record=True, color_system="standard")
    styles = LEVEL_STYLES + list(graph.STATUS_STYLES.values()) + [graph.GROUP_STYLE, graph.DISPATCH_STYLE]

    for style in styles:
        console.print(Text("palette", style=style), end="")

    assert console.export_text() == "palette" * len(styles)


def test_clip_truncates_long_names():
    assert _clip("short") == "short"
    assert _clip("x" * 40).endswith("…")
    assert cell_len(_clip("x" * 40)) <= MAXNAME


def test_clip_uses_terminal_cells_and_keeps_graphemes_intact():
    wide = _clip("界" * 30)
    combining = _clip("e\u0301" * 50)
    assert wide.endswith("…") and cell_len(wide) <= MAXNAME
    assert combining.endswith("…") and cell_len(combining) == MAXNAME
    assert combining.endswith("e\u0301…")


def test_clip_does_not_split_flags_variation_sequences_or_zwj_emoji():
    too_close = "x" * 36
    fits = "x" * 35
    clusters = ["🇺🇸", "✈️", "👨\u200d👩\u200d👧\u200d👦"]
    for cluster in clusters:
        assert _clip(too_close + cluster + "tail") == too_close + "…"
        assert _clip(fits + cluster + "tail") == fits + cluster + "…"
    assert _clip(too_close + "e\u0301tail") == too_close + "e\u0301…"


def test_wrap_uses_terminal_cells_for_wide_and_combining_text():
    wide = wrap_text("界" * 25, 15)
    combining = _wrap("e\u0301" * 25, 20)
    assert "".join(wide) == "界" * 25
    assert "".join(combining) == "e\u0301" * 25
    assert all(cell_len(line) <= 15 for line in wide)
    assert all(cell_len(line) <= 20 for line in combining)


def test_sanitize_text_preserves_layout_controls_and_neutralizes_terminal_controls():
    hostile = "ok\nnext\tcol\x00\x1b]52;c;payload\x07\x9b31m\r"
    safe = sanitize_text(hostile)
    assert safe.startswith("ok\nnext\tcol")
    assert "\x1b" not in safe and "\x07" not in safe and "\r" not in safe
    assert safe.count("�") == 5
    _assert_terminal_safe(safe)
    assert sanitize_text(None) == ""
    assert sanitize_text(42) == "42"


def test_sanitizers_repair_surrogates_and_inline_layout():
    assert sanitize_text("a\ud800b\udc00c") == "a�b�c"
    assert sanitize_text("\ud83d\ude00") == "😀"
    assert sanitize_text("a\ud800b").encode("utf-8") == b"a\xef\xbf\xbdb"
    inline = sanitize_inline_text("a\tb\nc\rd\ve\ff\x85g\u2028h\u2029i\x1bj\ud800")
    assert inline == "a b c d e f g h i�j�"
    assert "\n" not in inline and "\t" not in inline
    _assert_terminal_safe(inline)


def test_short_prefers_specific_label_else_task_line():
    assert _short(Node(id="main", label="main loop"), 0) == "main"
    assert _short(Node(id="a", label="code-reviewer"), 1) == "code-reviewer"
    # A generic label falls back to the node's task/detail line.
    assert _short(Node(id="a", label="general-purpose", detail="fix the bug"), 1) == "fix the bug"
    # Nothing useful at all -> stable positional name.
    assert _short(Node(id="a", label="agent"), 3) == "a3"


def test_batch_render_keeps_goal_label_before_status_counts():
    model = ClaudeModel("/nonexistent.jsonl")
    model.nodes["batch"] = Node(
        id="batch", label="Review parser", parent="main", kind="dispatch",
        children=["docs", "tests"],
    )
    model.nodes["docs"] = Node(id="docs", label="writer", parent="batch", status="done")
    model.nodes["tests"] = Node(id="tests", label="tester", parent="batch", status="running")
    model.nodes["main"].children = ["batch"]

    text, _, _, _ = render_tree(model, "main")

    assert "Review parser · 2 agents · 1 running · 1 done" in text.plain


def _model_with_tree():
    m = ClaudeModel("/nonexistent.jsonl")
    m.nodes["c1"] = Node(id="c1", label="reviewer", parent="main", status="done")
    m.nodes["c2"] = Node(id="c2", label="builder", parent="main", status="running")
    m.nodes["main"].children = ["c1", "c2"]
    m.nodes["gc"] = Node(id="gc", label="helper", parent="c2", status="done")
    m.nodes["c2"].children = ["gc"]
    return m


def test_render_tree_view_relationships_and_cell_coordinates():
    m = _model_with_tree()
    m.nodes["c1"].label = "reviewer"
    text, width, order, view = render_tree(m, "main")
    assert order == ["main", "c1", "c2", "gc"]
    assert view["main"].parent is None
    assert view["main"].children == ["c1", "c2"]
    assert view["c2"].children == ["gc"]
    assert view["gc"].parent == "c2" and view["gc"].depth == 2
    assert view["main"].x == 0
    assert view["c1"].x == 3
    assert view["gc"].x == 6
    assert view["c1"].w == 4 + cell_len("reviewer")
    assert width == max(12, max(cell_len(line) for line in text.plain.splitlines()) + 2)


def test_render_tree_collapsed_count_and_view_pruning():
    m = _model_with_tree()
    text, _, order, view = render_tree(m, "main", collapsed={"c2"})
    assert order == ["main", "c1", "c2"]
    assert "builder  +1" in text.plain
    assert view["c2"].children == []
    assert "gc" not in view


def test_render_tree_selected_label_has_reverse_style():
    m = _model_with_tree()
    text, _, _, _ = render_tree(m, "main", selected="c1")
    assert any(span.style == "reverse bold bright_yellow"
               and text.plain[span.start:span.end] == "reviewer"
               for span in text.spans)


def test_render_tree_status_icons_and_structural_groups():
    m = ClaudeModel("/nonexistent.jsonl")
    m.nodes["main"].children = ["batch", "idle"]
    m.nodes["batch"] = Node(
        "batch", "group row", parent="main", kind="dispatch", status="running",
        children=["done", "active", "failed"],
    )
    m.nodes["done"] = Node("done", "done agent", parent="batch", status="done")
    m.nodes["active"] = Node(
        "active", "active agent", parent="batch", status="running",
    )
    m.nodes["failed"] = Node(
        "failed", "failed agent", parent="batch", status="error",
    )
    m.nodes["idle"] = Node("idle", "idle agent", parent="main", status="waiting")

    text, _, _, _ = render_tree(m, "main")

    def styles_for(value):
        return {
            str(span.style)
            for span in text.spans
            if text.plain[span.start:span.end] == value
        }

    assert styles_for("group row · 3 agents · 1 running · 1 done · 1 error") == {
        "grey82"
    }
    assert RUNNING_GLYPH + " ╭─ ◆ group row" in text.plain
    assert "grey82" in styles_for("╭─ ")
    assert "grey82" in styles_for("◆ ")
    assert "grey82" in styles_for(" ─╮")
    assert styles_for(RUNNING_GLYPH + " ") == {"yellow"}
    assert styles_for("✓ ") == {"green"}
    assert styles_for("✗ ") == {"red"}
    assert styles_for("… ") == {"bright_black"}
    assert styles_for("done agent") == {"light_slate_blue"}
    assert styles_for("active agent") == {"light_slate_blue"}
    assert styles_for("failed agent") == {"light_slate_blue"}
    assert styles_for("idle agent") == {"light_slate_blue"}


def test_agent_status_colors_change_glyph_only():
    model = ClaudeModel("/nonexistent.jsonl")
    model.nodes["main"].status = "done"
    model.nodes["main"].children = ["task"]
    model.nodes["task"] = Node(
        "task", "ship work", parent="main", kind="task", status="completed",
    )

    tree, _, _, _ = render_tree(model, "main")
    assert any(
        span.style == "green" and tree.plain[span.start:span.end] == "✓ "
        for span in tree.spans
    )
    assert any(
        span.style == "white" and tree.plain[span.start:span.end] == "main"
        for span in tree.spans
    )
    assert any(
        span.style == "white" and tree.plain[span.start:span.end] == "ship work"
        for span in tree.spans
    )

    actions = Text()
    render_actions(
        actions,
        [Step("tool", "Bash: failed command", status="error")],
        selected=None,
        expanded=set(),
        depth=0,
    )
    assert any(
        span.style == "red" and actions.plain[span.start:span.end] == "✗ "
        for span in actions.spans
    )
    assert any(
        span.style == "bold white"
        and actions.plain[span.start:span.end] == "Bash: "
        for span in actions.spans
    )

    board = Text()
    render_task_board(board, [Task("1", "ship work", status="completed")])
    assert any(
        span.style == "white"
        and board.plain[span.start:span.end].strip() == "#1 ship work"
        for span in board.spans
    )


def test_render_tree_role_color_does_not_change_with_status_or_depth():
    m = _model_with_tree()
    m.nodes["gc"].label = "review helper"
    m.nodes["gc"].status = "running"
    text, _, _, _ = render_tree(m, "main")
    label_styles = {
        text.plain[span.start:span.end]: str(span.style)
        for span in text.spans
        if text.plain[span.start:span.end] in {"reviewer", "review helper"}
    }
    assert label_styles == {
        "reviewer": "bright_yellow",
        "review helper": "bright_yellow",
    }


def test_render_tree_uses_role_colors_for_every_subagent_type():
    model = ClaudeModel("/nonexistent.jsonl")
    roles = (
        ("research", "Research parser", "bright_blue"),
        ("design", "Design parser", "medium_turquoise"),
        ("code", "Implement parser", "pale_turquoise1"),
        ("tests", "Write parser tests", "orchid1"),
        ("review", "Review parser", "bright_yellow"),
        ("debug", "Debug parser", "bright_red"),
        ("repair", "Fix parser", "pale_turquoise1"),
        ("docs", "Document parser", "bright_white"),
        ("coordinate", "Coordinate parser", "bright_magenta"),
        ("other", "Handle parser", "light_slate_blue"),
    )
    model.nodes["main"].children = [role for role, _, _ in roles]
    model.nodes["main"].steps = [
        Step("spawn", "Agent", body=purpose, child=role)
        for role, purpose, _ in roles
    ]
    for role, _, _ in roles:
        model.nodes[role] = Node(role, role + " worker", parent="main")

    text, _, _, _ = render_tree(model, "main")
    label_styles = {
        text.plain[span.start:span.end]: str(span.style)
        for span in text.spans
        if text.plain[span.start:span.end].endswith(" worker")
    }

    assert label_styles == {
        role + " worker": style for role, _, style in roles
    }


def test_render_tree_lists_present_role_labels_like_timeline():
    model = ClaudeModel("/nonexistent.jsonl")
    model.nodes["main"].children = ["research", "review"]
    model.nodes["research"] = Node(
        "research", "researcher", parent="main",
    )
    model.nodes["review"] = Node(
        "review", "reviewer", parent="main",
    )
    model.nodes["hidden"] = Node(
        "hidden", "designer", parent="elsewhere",
    )

    text, _, _, _ = render_tree(model, "main")

    assert text.plain.splitlines()[0] == "roles ⌕ research · ◎ review"
    assert "◇ design" not in text.plain


def test_render_tree_folds_role_legend_within_available_width():
    model = ClaudeModel("/nonexistent.jsonl")
    labels = [
        "researcher", "designer", "coder", "tester", "reviewer",
        "debugger", "docs writer", "coordinator",
    ]
    model.nodes["main"].children = labels
    for label in labels:
        model.nodes[label] = Node(label, label, parent="main")

    text, width, _, view = render_tree(model, "main", available_width=30)
    lines = text.plain.splitlines()
    legend_height = view["main"].row

    assert legend_height > 1
    assert all(cell_len(line) <= 28 for line in lines[:legend_height])
    assert lines[legend_height] == "▶ main"
    assert width <= 30


def test_tree_shows_subagent_role_icons():
    model = ClaudeModel("/nonexistent.jsonl")
    model.nodes["main"].steps = [
        Step("spawn", "Agent", body="Write parser tests", child="tests"),
        Step("spawn", "Agent", body="Review parser diff", child="review"),
        Step("spawn", "Agent", body="Fix parser crash", child="fix"),
    ]
    model.nodes["main"].children = ["tests", "review", "fix"]
    model.nodes["tests"] = Node(
        "tests", "test writer", parent="main", status="done",
    )
    model.nodes["review"] = Node(
        "review", "reviewer", parent="main", status="running",
    )
    model.nodes["fix"] = Node(
        "fix", "fixer", parent="main", status="done",
    )

    rendered, _, _, _ = render_tree(model, "main")

    assert "✓ ⚗ test writer" in rendered.plain
    assert "▶ ◎ reviewer" in rendered.plain
    assert "✓ ⌨ fixer" in rendered.plain


def test_render_tree_neutralizes_terminal_controls():
    m = _model_with_tree()
    m.nodes["c1"].label = "review\x1b]52;c;payload\x07\x00\x9b"
    text, _, _, _ = render_tree(m, "main")
    _assert_terminal_safe(text.plain)
    assert "payload" in text.plain


def test_render_tree_normalizes_inline_newlines_tabs_and_surrogates():
    m = _model_with_tree()
    m.nodes["c1"].label = "review\nnext\tthing\ud800"
    text, width, order, view = render_tree(m, "main")
    assert order == ["main", "c1", "c2", "gc"]
    assert "review next thing�" in text.plain
    assert len(text.plain.splitlines()) == len(order) + 1
    assert [view[node_id].row for node_id in order] == [1, 2, 3, 4]
    assert width == max(12, max(cell_len(line) for line in text.plain.splitlines()) + 2)
    assert view["c1"].w == 4 + cell_len("review next thing�")
    _assert_terminal_safe(text.plain)


def test_render_tree_stop_kinds_prunes_children():
    m = _model_with_tree()
    m.nodes["c2"].kind = "team"
    _, _, order, _ = render_tree(m, "main", stop_kinds=("team",))
    assert order == ["main", "c1", "c2"]          # c2's child not expanded
    assert "gc" not in order


def test_render_tree_missing_root_is_graceful():
    m = ClaudeModel("/nonexistent.jsonl")
    text, width, order, _view = render_tree(m, "nope")
    assert order == []
    assert "nothing running" in text.plain


def test_render_task_board_renders_status_owner_and_cell_aligned_rows():
    tasks = [
        Task(id="1", subject="修复界面", status="in_progress", owner="开发者"),
        Task(id="2", subject="test", status="completed"),
        Task(id="3", subject="later", status="pending", owner="sam"),
    ]
    out = Text()
    width = render_task_board(out, tasks)
    rows = out.plain.splitlines()[2:]
    assert "tasks" in out.plain
    assert "▨ #1 修复界面" in rows[0] and "→ 开发者  in_progress" in rows[0]
    assert "▣ #2 test" in rows[1] and "→ —  completed" in rows[1]
    assert "▢ #3 later" in rows[2] and "→ sam  pending" in rows[2]
    assert len({row.index("→") for row in rows}) > 1  # code-point offsets differ
    assert len({cell_len(row.split("→", 1)[0]) for row in rows}) == 1
    assert width == max(cell_len(row) for row in rows)


def test_render_task_board_clips_wide_labels_and_neutralizes_controls():
    tasks = [Task(id="\x1b1", subject="界" * 30 + "\x07", status="bad\x9b", owner="o\x00")]
    out = Text()
    width = render_task_board(out, tasks)
    row = out.plain.splitlines()[2]
    _assert_terminal_safe(out.plain)
    label = row[2:row.index("  → ")].rstrip()
    assert label.endswith("…") and cell_len(label) <= MAXNAME
    assert width == cell_len(row)


def test_render_task_board_normalizes_inline_layout_and_preserves_width():
    task = Task(id="1\n2", subject="fix\ttabs", status="in\nprogress\ud800",
                owner="sam\rdev")
    out = Text()
    width = render_task_board(out, [task])
    rows = out.plain.splitlines()
    assert rows == ["", "tasks", "▢ #1 2 fix tabs  → sam dev  in progress�"]
    assert width == cell_len(rows[-1])
    _assert_terminal_safe(out.plain)


def test_render_actions_rows_expansion_output_status_and_selection():
    steps = [
        Step("thinking", "thinking: plan", "line one\nline two"),
        Step("tool", "Bash: run", "echo hi", status="error", output="failed\nbad"),
        Step("spawn", "Agent: helper", "inspect", ts=99, status="running"),
    ]
    out = Text()
    rowmap = []
    selected = render_actions(
        out, steps, 1, {0, 1, 2}, depth=1, rows_out=rowmap, width=40
    )
    assert selected == (4, 8)
    assert rowmap == [(1, 0), (4, 1), (9, 2)]
    assert "✶ thinking: plan" in out.plain
    assert "✗ Bash: run  error" in out.plain
    assert RUNNING_GLYPH + " Agent: helper  running" in out.plain
    assert "— output —\n" in out.plain and "failed\n" in out.plain
    assert "— instructions —\n" in out.plain and "inspect\n" in out.plain
    assert any(span.style == "reverse bold" and out.plain[span.start:span.end] == "Bash: run"
               for span in out.spans)


def test_render_actions_wraps_text_body_by_cell_width():
    out = Text()
    render_actions(out, [Step("text", "assistant: reply", "界" * 25)], 0, {0},
                   depth=0, width=20)
    body_rows = out.plain.splitlines()[2:]
    assert len(body_rows) == 4
    assert all(cell_len(row) <= 20 for row in body_rows)
    assert "".join(row.removeprefix("     ") for row in body_rows) == "界" * 25


def test_render_actions_neutralizes_controls_in_all_model_fields():
    step = Step("tool", "Bash\x1b]0;fake\x07: run", "body\x00\x9b31m",
                status="run\rning", output="out\x08\x1b\\")
    out = Text()
    render_actions(out, [step], 0, {0}, depth=0)
    _assert_terminal_safe(out.plain)
    assert "fake" in out.plain and "body" in out.plain and "out" in out.plain


def test_render_actions_inline_fields_cannot_change_row_mapping():
    steps = [
        Step("text", "assistant:\nreply\tnow\ud800", "line one\nline two"),
        Step("tool", "Bash:\r run", status="run\nning"),
    ]
    out = Text()
    rowmap = []
    selected = render_actions(out, steps, 0, {0}, depth=0, rows_out=rowmap)
    assert rowmap == [(1, 0), (4, 1)]
    assert selected == (1, 3)
    assert "assistant: reply now�" in out.plain
    assert "Bash:  run  run ning" in out.plain
    assert len(out.plain.splitlines()) == 5
    _assert_terminal_safe(out.plain)


def test_skill_name_shown_on_tree_line():
    m = _model_with_tree()
    m.nodes["c1"].skills = ["tdd", "code-review"]
    text, _, _, _ = render_tree(m, "main", skills=True)
    line = next(l for l in text.plain.splitlines() if "reviewer" in l)
    assert "⟡ code-review" in line          # latest skill, on the node's own line
    assert "tdd" not in text.plain
