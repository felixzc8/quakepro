from types import SimpleNamespace

from rich.cells import cell_len

from quakepro.graph import render_timeline
from quakepro.session_model import Node, Step
from quakepro.session_story import (
    SessionStory,
    SessionStoryProjector,
    SessionTimeline,
    StoryAgent,
    TimelineLane,
    project_session_story,
    project_session_timeline,
)


class CountingSteps(list):
    def __init__(self, values=()):
        super().__init__(values)
        self.reads = 0

    def __getitem__(self, index):
        self.reads += 1
        return super().__getitem__(index)


def story_agents(story):
    return {agent.id: agent for agent in story.agents}


def style_at(rendered, value, start=None):
    offset = (
        rendered.plain.rindex(value)
        if start is None else rendered.plain.index(value, start)
    )
    return next(
        str(span.style)
        for span in reversed(rendered.spans)
        if span.start <= offset < span.end
    )


def test_incremental_projection_reads_only_step_suffix_and_reuses_old_agents():
    root_steps = CountingSteps([
        Step("spawn", "Agent", body="Build ticket 8", child="one", ts=1),
        Step("spawn", "Agent", body="Build ticket 9", child="two", ts=2),
    ])
    one_steps = CountingSteps([Step("tool", "Read", body="old", ts=3)])
    two_steps = CountingSteps([Step("tool", "Read", body="old", ts=4)])
    nodes = {
        "main": Node(
            "main", "main", kind="main", children=["one", "two"],
            steps=root_steps,
        ),
        "one": Node("one", "one", parent="main", steps=one_steps),
        "two": Node("two", "two", parent="main", steps=two_steps),
    }
    projector = SessionStoryProjector()
    before = story_agents(projector.project(SimpleNamespace(nodes=nodes), "main"))
    root_steps.reads = one_steps.reads = two_steps.reads = 0

    two_steps.append(Step("tool", "Read", body="new", ts=5))
    nodes["two"].status = "done"
    after = story_agents(projector.project(SimpleNamespace(nodes=nodes), "main"))

    assert root_steps.reads == 0
    assert one_steps.reads == 0
    assert two_steps.reads == 1
    assert after["one"] is before["one"]
    assert after["two"] is not before["two"]


def test_incremental_projection_does_not_reclassify_from_appended_prompt():
    nodes = {
        "main": Node("main", "main", kind="main", children=["worker"]),
        "worker": Node("worker", "worker", parent="main"),
    }
    projector = SessionStoryProjector()

    before = projector.project(SimpleNamespace(nodes=nodes), "main")
    nodes["worker"].steps.append(
        Step("prompt", "Prompt", body="Review ticket 9", ts=2),
    )
    after = projector.project(SimpleNamespace(nodes=nodes), "main")

    assert before.agents[0].label == "worker"
    assert before.agents[0].role == "other"
    assert after.agents[0].label == "worker"
    assert after.agents[0].role == "other"


def test_agent_projection_is_provider_neutral():
    nodes = {
        "main": Node(
            "main", "main", kind="main", children=["worker"],
            steps=[Step(
                "spawn", "Agent", body="Build ticket 8", child="worker", ts=10,
            )],
        ),
        "worker": Node(
            "worker", "builder", parent="main", status="done", last_ts=20,
        ),
    }

    stories = [
        project_session_story(SimpleNamespace(provider=provider, nodes=nodes), "main")
        for provider in ("claude", "codex", "pi")
    ]

    assert stories[0] == stories[1] == stories[2]
    assert [agent.id for agent in stories[0].agents] == ["worker"]


def test_each_agent_gets_one_lane_without_work_grouping():
    nodes = {
        "main": Node(
            "main", "main", kind="main", children=["batch"],
            steps=[
                Step("prompt", "task", body="Improve monitor", ts=1),
                Step("spawn", "Agent", body="Build ticket 8", child="build", ts=10),
                Step("spawn", "Agent", body="Review ticket 8", child="review", ts=20),
                Step("spawn", "Agent", body="Fix whole session", child="fix", ts=30),
            ],
        ),
        "batch": Node(
            "batch", "batch", parent="main", kind="dispatch",
            children=["build", "review", "fix"],
        ),
        "build": Node("build", "builder", parent="batch", status="done", last_ts=15),
        "review": Node("review", "reviewer", parent="batch", status="done", last_ts=25),
        "fix": Node("fix", "fixer", parent="batch", status="running", last_ts=35),
    }

    story = project_session_story(SimpleNamespace(nodes=nodes), "main")
    timeline = project_session_timeline(story)

    assert [agent.id for agent in story.agents] == ["build", "review", "fix"]
    assert [lane.label for lane in timeline.lanes] == ["builder", "reviewer", "fixer"]
    assert [lane.agent_id for lane in timeline.lanes] == ["build", "review", "fix"]


def test_structural_nodes_stay_out_while_nested_agents_get_lanes():
    nodes = {
        "main": Node("main", "main", kind="main", children=["batch"]),
        "batch": Node(
            "batch", "batch", parent="main", kind="dispatch", children=["parent"],
        ),
        "parent": Node(
            "parent", "parent agent", parent="batch", children=["child"],
        ),
        "child": Node("child", "child agent", parent="parent"),
    }

    story = project_session_story(SimpleNamespace(nodes=nodes), "main")

    assert [agent.id for agent in story.agents] == ["parent", "child"]


def test_latest_activity_without_start_evidence_stays_untimed():
    nodes = {
        "main": Node("main", "main", kind="main", children=["worker"]),
        "worker": Node(
            "worker", "worker", parent="main", status="running", last_ts=30,
        ),
    }

    timeline = project_session_timeline(
        project_session_story(SimpleNamespace(nodes=nodes), "main"))

    assert timeline.lanes[0].started_at == 0
    assert timeline.lanes[0].latest_at == 30
    assert timeline.lanes[0].cues == ["active", "missing-time"]


def test_timeline_shows_each_agent_role_icon():
    story = SessionStory([
        StoryAgent("tests", "test writer", "main", "done", 10, 20, "tests"),
        StoryAgent("review", "reviewer", "main", "done", 20, 30, "review"),
    ], "done")

    rendered, _ = render_timeline(project_session_timeline(story), 88)

    assert "⚗ tests" in rendered.plain
    assert "◎ review" in rendered.plain
    assert "test writer" in rendered.plain
    assert "reviewer" in rendered.plain


def test_timeline_colors_are_stable_and_failures_are_red():
    story = SessionStory([
        StoryAgent("first", "first", "main", "done", 10, 15, "code"),
        StoryAgent("second", "second", "main", "done", 20, 25, "review"),
        StoryAgent("failed", "failed", "main", "error", 30, 35, "code"),
    ], "error")
    timeline = project_session_timeline(story)

    for width in (88, 48, 19):
        rendered, _ = render_timeline(timeline, width)
        assert style_at(rendered, "first") == "pale_turquoise1"
        assert style_at(rendered, "second") == "bright_yellow"
        assert style_at(rendered, "failed") == "pale_turquoise1"
        assert style_at(rendered, "✗") == "red"
        if width == 48:
            assert style_at(rendered, "error") == "bright_black"

    wide, _ = render_timeline(timeline, 88)
    failed_label = wide.plain.rindex("failed")
    assert style_at(wide, "─", failed_label) == "bright_black"
    assert style_at(wide, "✗", failed_label) == "red"


def test_timeline_projects_agent_runs_on_shared_scale_with_recorded_cues():
    nodes = {
        "main": Node(
            "main", "main", kind="main",
            children=["build", "other", "review", "fix", "point", "unknown"],
            steps=[
                Step("spawn", "Agent", body="Build", child="build", ts=10),
                Step("spawn", "Agent", body="Build", child="other", ts=20),
                Step("spawn", "Agent", body="Review", child="review", ts=31),
                Step("spawn", "Agent", body="Fix", child="fix", ts=40),
                Step("spawn", "Agent", body="Build", child="point", ts=70),
            ],
        ),
        "build": Node("build", "builder", parent="main", status="done", last_ts=30),
        "other": Node("other", "other", parent="main", status="done", last_ts=45),
        "review": Node("review", "reviewer", parent="main", status="error", last_ts=40),
        "fix": Node("fix", "fixer", parent="main", status="running", last_ts=60),
        "point": Node("point", "point", parent="main", status="done"),
        "unknown": Node("unknown", "unknown", parent="main", status="waiting"),
    }

    timeline = project_session_timeline(
        project_session_story(SimpleNamespace(nodes=nodes), "main"))
    lanes = {lane.label: lane for lane in timeline.lanes}

    assert (timeline.started_at, timeline.latest_at) == (10, 70)
    assert lanes["builder"].cues == ["parallel"]
    assert lanes["other"].cues == ["parallel"]
    assert lanes["reviewer"].cues == ["parallel", "failed"]
    assert lanes["fixer"].cues == ["parallel", "active"]
    assert lanes["point"].cues == []
    assert lanes["unknown"].cues == ["missing-time"]


def test_timeline_numbers_parallel_sections_by_recorded_time():
    story = SessionStory([
        StoryAgent("early-a", "early a", "main", "done", 10, 30),
        StoryAgent("early-b", "early b", "main", "done", 15, 25),
        StoryAgent("solo", "solo", "main", "done", 35, 40),
        StoryAgent("late-a", "late a", "main", "done", 50, 70),
        StoryAgent("late-b", "late b", "main", "done", 55, 65),
        StoryAgent("boundary", "boundary", "main", "done", 70, 80),
    ], "done")

    lanes = project_session_timeline(story).lanes

    assert [lane.parallel_section for lane in lanes] == [0, 0, None, 1, 1, None]


def test_timeline_parallel_sections_progress_through_blue_hues():
    story = SessionStory([
        StoryAgent("early-a", "early a", "main", "done", 10, 30, "code"),
        StoryAgent("early-b", "early b", "main", "done", 15, 25, "code"),
        StoryAgent("solo", "solo", "main", "done", 35, 40, "code"),
        StoryAgent("late-a", "late a", "main", "done", 50, 70, "code"),
        StoryAgent("late-fail", "late fail", "main", "error", 55, 65, "code"),
    ], "error")
    rendered, _ = render_timeline(project_session_timeline(story), 88)

    def chart_style(label, mark):
        return style_at(rendered, mark, rendered.plain.index(label))

    assert chart_style("early a", "━") == "#afd7ff"
    assert chart_style("early b", "━") == "#afd7ff"
    assert chart_style("solo", "━") == "bright_black"
    assert chart_style("late a", "━") == "#5f5fff"
    assert chart_style("late fail", "─") == "#5f5fff"
    assert chart_style("late fail", "✗") == "red"


def test_timeline_treats_boundary_touching_agents_as_sequential():
    story = SessionStory([
        StoryAgent("first", "first", "main", "done", 10, 20),
        StoryAgent("second", "second", "main", "done", 20, 30),
    ], "done")

    lanes = {
        lane.label: lane for lane in project_session_timeline(story).lanes
    }

    assert lanes["first"].cues == []
    assert lanes["second"].cues == []


def test_timeline_respects_positive_width_bounds():
    story = SessionStory([
        StoryAgent("build", "builder", "main", "done", 10, 25, "code"),
        StoryAgent("review", "reviewer", "main", "error", 30, 40, "review"),
        StoryAgent("helper", "helper", "main", "done", 35, 35, "code"),
        StoryAgent("fix", "fixer", "main", "running", 40, 60, "code"),
        StoryAgent("other", "other", "main", "done", 20, 45, "code"),
        StoryAgent("point", "point", "main", "done", 70, 70, "code"),
        StoryAgent("unknown", "unknown", "main", "waiting", 0, 0),
    ], "error")
    timeline = project_session_timeline(story)

    wide, wide_width = render_timeline(timeline, 88)
    narrow, narrow_width = render_timeline(timeline, 48)
    tiny, tiny_width = render_timeline(timeline, 20)

    assert "Timeline · 7 agents" in wide.plain
    for cue in (
        "║ parallel", "▶ active", "✗ failed",
        "? untimed",
    ):
        assert cue in wide.plain
    assert wide_width <= 88
    assert max(cell_len(line) for line in wide.plain.splitlines()) <= 88

    for label in (
        "builder", "reviewer", "helper", "fixer", "other", "point", "unknown",
    ):
        assert label in narrow.plain
    for cue in (
        "║:parallel", "▶:active", "✗:failed",
        "?:untimed",
    ):
        assert cue in narrow.plain
    assert narrow_width <= 48
    assert max(cell_len(line) for line in narrow.plain.splitlines()) <= 48
    assert sum(line[:2].isdigit() for line in tiny.plain.splitlines()) == 7
    assert tiny_width <= 20
    assert max(cell_len(line) for line in tiny.plain.splitlines()) <= 20

    for boundary in (19, 8, 1):
        compact, compact_width = render_timeline(timeline, boundary)
        lines = compact.plain.splitlines()
        if boundary == 19:
            assert lines[0] == "T 7"
        assert compact_width <= boundary
        assert max(cell_len(line) for line in lines) <= boundary


def test_timeline_narrow_rows_keep_agents_in_order():
    timeline = SessionTimeline([
        TimelineLane("first", cues=["parallel"]),
        TimelineLane("second"),
        TimelineLane("third"),
    ])

    for boundary in (8, 3, 1):
        rendered, _ = render_timeline(timeline, boundary)
        rows = rendered.plain.replace("\n", "")
        assert rows.index("first") < rows.index("second") < rows.index("third")


def test_timeline_narrow_rows_keep_every_cue_mark():
    timeline = SessionTimeline([
        TimelineLane(
            "agent",
            cues=[
                "parallel", "active", "failed", "missing-time",
            ],
        ),
    ])

    for boundary in (8, 3, 1):
        rendered, drawn = render_timeline(timeline, boundary)
        assert all(mark in rendered.plain for mark in "║▶✗?")
        assert drawn <= boundary
        assert max(cell_len(line) for line in rendered.plain.splitlines()) <= boundary


def test_timeline_help_has_words_at_twenty_cells_and_marks_at_nineteen():
    timeline = SessionTimeline([
        TimelineLane(
            "agent",
            cues=[
                "parallel", "active", "failed", "missing-time",
            ],
        ),
    ])

    at_threshold, threshold_width = render_timeline(timeline, 20)
    below_threshold, below_width = render_timeline(timeline, 19)

    for word in (
        "parallel", "active", "failed", "untimed",
    ):
        assert word in at_threshold.plain
        assert word not in below_threshold.plain
    for mark in "║▶✗?":
        assert mark in below_threshold.plain
    assert threshold_width <= 20
    assert below_width <= 19


def test_timeline_help_only_explains_marks_present_in_session():
    timeline = project_session_timeline(SessionStory([
        StoryAgent("agent", "agent", "main", "error", 10, 20),
    ]))

    rendered, width = render_timeline(timeline, 88)

    assert "✗ failed" in rendered.plain
    assert "║ parallel" not in rendered.plain
    assert "▶ active" not in rendered.plain
    assert "? untimed" not in rendered.plain
    assert width <= 88


def test_timeline_zero_length_lane_uses_status_endpoint_without_cue():
    timeline = project_session_timeline(SessionStory([
        StoryAgent("agent", "agent", "main", "done", 10, 10),
    ]))

    rendered, width = render_timeline(timeline, 88)

    assert "cues " not in rendered.plain
    assert "◆" not in rendered.plain
    assert "●" in rendered.plain
    assert width <= 88
