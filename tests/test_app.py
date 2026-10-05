import asyncio
from pathlib import Path
import subprocess
import sys
import textwrap
import threading
import time
from unittest.mock import Mock

import pytest
from textual.widgets import Footer

import quakepro.app as app_module
from quakepro.app import QuakePro, ShortcutsScreen, _tabid
from rich.cells import cell_len
from quakepro.session_model import Node, Step
from quakepro.team_messaging import SendResult, TeammateTarget


class FakeModel:
    provider = "claude"

    def __init__(self):
        self.nodes = {
            "main": Node("main", "main loop", kind="main",
                         children=["agent-main", "wf:a-b", "wf:a_b", "team:red"]),
            "agent-main": Node(
                "agent-main", "main agent", parent="main", detail="do the work",
                steps=[Step("text", "TEXT: update", body="an update")],
            ),
            "wf:a-b": Node("wf:a-b", "⚙ first workflow", parent="main", kind="workflow"),
            "wf:a_b": Node("wf:a_b", "⚙ second workflow", parent="main", kind="workflow"),
            "team:red": Node(
                "team:red", "red team", parent="main", kind="team",
                children=["mate-one", "mate-two"],
            ),
            "mate-one": Node("mate-one", "one", parent="team:red", kind="teammate"),
            "mate-two": Node("mate-two", "two", parent="team:red", kind="teammate"),
        }
        self.task_list = {}

    def poll(self):
        return None

    def observation_paths(self):
        return {str(Path(__file__).parent)}


class FakeObservation:
    def __init__(self, model):
        self.model = model
        self.events = asyncio.Queue()
        self.reconciles = 0

    def reconcile(self):
        self.reconciles += 1
        return bool(self.model.poll())

    async def changes(self):
        while True:
            yield await self.events.get()


def run_pilot(check):
    async def exercise():
        application = QuakePro("unused", model=FakeModel())
        async with application.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            await check(application, pilot)

    asyncio.run(exercise())


class MemoryMessenger:
    def __init__(self, result=None):
        self.result = result or SendResult(True, "one")
        self.sent = []

    def target_for(self, model, node_id):
        if node_id == "mate-one":
            return TeammateTarget("red", "one")
        if node_id == "mate-two":
            return TeammateTarget("red", "two")
        return None

    def send(self, target, text):
        self.sent.append((target, text))
        return self.result


def dispatch_model():
    model = FakeModel()
    model.provider = "codex"
    model.nodes = {
        "main": Node(
            "main", "Codex main", kind="main",
            children=["dispatch:done", "dispatch:active", "dispatch:error"],
        ),
    }
    for batch_id, status in (
        ("dispatch:done", "done"),
        ("dispatch:active", "running"),
        ("dispatch:error", "error"),
    ):
        children = [f"{batch_id}:{index}" for index in range(4)]
        model.nodes[batch_id] = Node(
            batch_id, batch_id.removeprefix("dispatch:"),
            parent="main", status=status, kind="dispatch", children=children,
        )
        for child_id in children:
            model.nodes[child_id] = Node(
                child_id, child_id.rsplit(":", 1)[-1],
                parent=batch_id, status=status,
            )
    return model


def run_dispatch_pilot(check, model=None):
    async def exercise():
        application = QuakePro("unused", model=model or dispatch_model())
        async with application.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            await check(application, pilot)

    asyncio.run(exercise())


def test_main_honors_disable_marker(tmp_path, monkeypatch, capsys):
    (tmp_path / "DISABLED").touch()
    fake_source = tmp_path / "src" / "quakepro" / "app.py"
    monkeypatch.setattr(app_module, "Path", lambda _path: fake_source)
    app_class = Mock()
    monkeypatch.setattr(app_module, "QuakePro", app_class)

    assert app_module.main([]) == 0
    assert capsys.readouterr().err == "QuakePro is disabled.\n"
    app_class.assert_not_called()


def test_main_routes_overview_roots(monkeypatch):
    roots = ["/work/one", "/work/two"]
    resolved = ["/resolved/one", "/resolved/two"]
    model = object()
    resolve_roots = Mock(return_value=resolved)
    make_overview = Mock(return_value=model)
    application = Mock()
    app_class = Mock(return_value=application)
    monkeypatch.setattr(app_module, "resolve_roots", resolve_roots)
    monkeypatch.setattr(app_module, "RunOverview", make_overview)
    monkeypatch.setattr(app_module, "QuakePro", app_class)

    assert app_module.main([
        "--root", roots[0], "--root", roots[1], "--worktrees",
    ]) == 0
    resolve_roots.assert_called_once_with(roots, True)
    make_overview.assert_called_once_with(resolved)
    app_class.assert_called_once_with(resolved[0], model=model)
    application.run.assert_called_once_with()


def test_main_rejects_empty_overview(monkeypatch):
    monkeypatch.setattr(app_module, "resolve_roots", Mock(return_value=[]))

    with pytest.raises(SystemExit, match="QuakePro: no run roots found"):
        app_module.main(["--worktrees"])


def test_main_rejects_missing_session(monkeypatch):
    monkeypatch.setattr(app_module, "latest_session", Mock(return_value=None))

    with pytest.raises(SystemExit, match="QuakePro: no matching .* transcript found"):
        app_module.main([])


def test_main_reports_provider_mismatch(tmp_path, monkeypatch):
    session = tmp_path / "session.jsonl"
    session.touch()

    monkeypatch.setattr(
        app_module,
        "open_model",
        Mock(side_effect=ValueError("session is not a Codex rollout")),
    )

    with pytest.raises(
        SystemExit, match="QuakePro: session is not a Codex rollout"
    ):
        app_module.main(["--file", str(session), "--provider", "codex"])


def test_main_opens_session_and_runs_app(tmp_path, monkeypatch):
    session = tmp_path / "session.jsonl"
    session.touch()
    project = tmp_path / "project"
    project.mkdir()
    model = object()
    latest_session = Mock(return_value=str(session))
    open_model = Mock(return_value=model)
    application = Mock()
    app_class = Mock(return_value=application)
    monkeypatch.setattr(app_module, "latest_session", latest_session)
    monkeypatch.setattr(app_module, "open_model", open_model)
    monkeypatch.setattr(app_module, "QuakePro", app_class)

    assert app_module.main([
        "--provider", "pi", "--project", str(project),
    ]) == 0
    latest_session.assert_called_once_with(str(project), "pi")
    open_model.assert_called_once_with(
        str(session), "pi", project_root=str(project)
    )
    app_class.assert_called_once_with(str(session), model=model)
    application.run.assert_called_once_with()


def test_app_does_not_schedule_periodic_repaints():
    class RecordingQuakePro(QuakePro):
        def __init__(self, *args, **kwargs):
            self.scheduled_callbacks = []
            super().__init__(*args, **kwargs)

        def set_interval(self, interval, callback=None, **kwargs):
            self.scheduled_callbacks.append((interval, callback.__name__))
            return super().set_interval(interval, callback, **kwargs)

    async def exercise():
        application = RecordingQuakePro("unused", model=FakeModel())
        async with application.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            assert application.scheduled_callbacks == []

    asyncio.run(exercise())


def test_view_cycle_help_follows_view_metadata():
    binding = next(
        binding
        for binding in QuakePro.BINDINGS
        if (binding.key if hasattr(binding, "key") else binding[0]) == "v"
    )

    description = binding.description if hasattr(binding, "description") else binding[2]
    assert description == "/".join(app_module.VIEW_MODES.values())


def test_vim_navigation_keys():
    async def check(application, pilot):
        await pilot.press("j")
        assert (application.zone, application.selected) == ("graph", "main")
        await pilot.press("j")
        assert application.selected == "agent-main"
        await pilot.press("k")
        assert application.selected == "main"

        await pilot.press("G")
        assert application.selected == "team:red"
        await pilot.press("g", "g")
        assert application.selected == "main"

        await pilot.press("ctrl+d")
        assert application.selected == "team:red"
        await pilot.press("ctrl+u")
        assert application.selected == "main"

        await pilot.press("j", "]", "]")
        assert application.selected == "wf:a-b"
        await pilot.press("[", "[")
        assert application.selected == "agent-main"
        await pilot.press("g", "g")

        await pilot.press("g", "t")
        assert application.active_root == "wf:a-b"
        await pilot.press("g", "T")
        assert application.active_root == "main"

        await pilot.press("z", "M")
        assert application.collapsed == frozenset({"main"})
        await pilot.press("z", "o")
        assert application.collapsed == frozenset()
        await pilot.press("z", "a")
        assert application.collapsed == frozenset({"main"})
        await pilot.press("z", "R")
        assert application.collapsed == frozenset()

    run_pilot(check)


def test_shortcuts_help_page():
    async def check(application, pilot):
        before = application._navigation
        await pilot.press("?")

        assert isinstance(application.screen, ShortcutsScreen)
        assert "QuakePro shortcuts" in application.screen.query_one("#shortcuts").content.plain
        assert "gg / G" in application.screen.query_one("#shortcuts").content.plain

        await pilot.press("escape")
        assert not isinstance(application.screen, ShortcutsScreen)
        assert application._navigation == before

        await pilot.press("?", "?")
        assert not isinstance(application.screen, ShortcutsScreen)
        await pilot.press("?", "q")
        assert not isinstance(application.screen, ShortcutsScreen)
        assert application._navigation == before

    run_pilot(check)


def test_narrow_footer_hides_whole_shortcuts_and_restores_them_on_resize():
    async def exercise():
        application = QuakePro("unused", model=FakeModel())
        async with application.run_test(size=(91, 44)) as pilot:
            await pilot.pause()
            footer = application.query_one(Footer)
            palette = footer.query_one(".-command-palette")
            shortcuts = [
                key for key in footer.query("FooterKey")
                if key is not palette
            ]
            narrow_count = len(shortcuts)
            visible_bindings = sum(
                not hasattr(binding, "show") or binding.show
                for binding in QuakePro.BINDINGS
            )
            assert narrow_count < visible_bindings
            assert all(
                key.region.right <= palette.region.x
                for key in shortcuts
            )

            await pilot.resize_terminal(160, 44)
            await pilot.pause()
            palette = footer.query_one(".-command-palette")
            shortcuts = [
                key for key in footer.query("FooterKey")
                if key is not palette
            ]
            assert len(shortcuts) > narrow_count
            assert all(
                key.region.right <= palette.region.x
                for key in shortcuts
            )

            await pilot.resize_terminal(220, 44)
            await pilot.pause()
            assert len(footer.query("FooterKey")) == visible_bindings + 1

    asyncio.run(exercise())


def test_app_repaints_only_after_observation_event():
    async def exercise():
        model = FakeModel()
        observation = FakeObservation(model)
        application = QuakePro("unused", model=model, observation=observation)
        async with application.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            assert observation.reconciles == 1
            assert "main agent" in application._canvas.content.plain

            model.nodes["agent-main"].label = "event update"
            await pilot.pause()
            assert "event update" not in application._canvas.content.plain

            await observation.events.put(frozenset({"session.jsonl"}))
            await pilot.pause()
            assert "event update" in application._canvas.content.plain

    asyncio.run(exercise())


def test_mount_keeps_event_loop_free_during_initial_reconcile():
    started = threading.Event()
    release = threading.Event()
    released_at = []
    ticked_at = []

    class BlockingObservation(FakeObservation):
        def reconcile(self):
            self.reconciles += 1
            started.set()
            release.wait(timeout=1)
            return False

    def release_observation():
        started.wait(timeout=1)
        time.sleep(0.15)
        released_at.append(time.monotonic())
        release.set()

    class ProbeApp:
        _observe_session = QuakePro._observe_session

        def __init__(self, observation):
            self.observation = observation

        def _mark_updated(self):
            pass

        def sync_tabs(self):
            pass

        def render_canvas(self):
            pass

        def exit(self):
            pass

        def notify(self, *_args, **_kwargs):
            pass

    async def exercise():
        model = FakeModel()
        observation = BlockingObservation(model)
        application = ProbeApp(observation)
        releaser = threading.Thread(target=release_observation)
        releaser.start()
        task = asyncio.create_task(application._observe_session())
        while not started.is_set():
            await asyncio.sleep(0)
        await asyncio.sleep(0.02)
        ticked_at.append(time.monotonic())
        await asyncio.to_thread(releaser.join)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert ticked_at[0] < released_at[0]

    asyncio.run(exercise())


def test_quit_does_not_wait_for_blocked_reconcile_thread():
    program = textwrap.dedent("""
        import asyncio
        import sys
        import threading

        sys.path.insert(0, "src")

        from quakepro.app import QuakePro
        from quakepro.session_model import Node


        class Model:
            provider = "claude"
            nodes = {"main": Node("main", "main", kind="main")}

            def poll(self):
                return None

            def observation_paths(self):
                return set()


        class BlockingObservation:
            def __init__(self):
                self.started = threading.Event()

            def reconcile(self):
                self.started.set()
                threading.Event().wait()

            async def changes(self):
                while True:
                    yield None


        async def exercise():
            observation = BlockingObservation()
            application = QuakePro("unused", model=Model(), observation=observation)
            async with application.run_test(size=(100, 40)) as pilot:
                while not observation.started.is_set():
                    await asyncio.sleep(0)
                await pilot.press("q")


        asyncio.run(exercise())
    """)

    try:
        result = subprocess.run(
            [sys.executable, "-c", program],
            cwd=Path(__file__).parents[1],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except subprocess.TimeoutExpired:
        pytest.fail("quitting waited for blocked reconciliation thread")

    assert result.returncode == 0, result.stderr


def test_pilot_tabs_graph_drill_and_back():
    async def check(application, pilot):
        assert application.zone == "tabs"
        await pilot.press("down")
        assert application.zone == "graph"
        await pilot.press("enter")
        assert application.zone == "drill"
        assert application.drill_open
        await pilot.press("escape")
        assert application.zone == "graph"
        assert not application.drill_open
        await pilot.press("escape")
        assert application.zone == "tabs"

    run_pilot(check)


def test_drill_header_shows_subagent_role_icon():
    async def exercise():
        model = FakeModel()
        model.nodes["agent-main"].label = "main code agent"
        application = QuakePro("unused", model=model)
        async with application.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            application.selected = "agent-main"
            application._enter_drill()

            content = application._drillinstr.content
            header = content.plain
            assert "▶ ⌨ main code agent" in header
            assert " · code" in header
            styles = {
                content.plain[span.start:span.end]: str(span.style)
                for span in content.spans
            }
            assert styles["⌨ "] == "pale_turquoise1"
            assert styles["▶ "] == "yellow"
            assert styles["main code agent"] == "bold pale_turquoise1"

    asyncio.run(exercise())


def test_secondary_text_matches_action_detail_gray():
    def style_at(content, value):
        start = content.plain.index(value)
        end = start + len(value)
        return {
            str(span.style)
            for span in content.spans
            if span.start <= start and span.end >= end
        }

    async def exercise():
        model = FakeModel()
        agent = model.nodes["agent-main"]
        agent.label = "research agent"
        agent.workflow = ["agent dispatched"]
        agent.result = "finished work"
        application = QuakePro("unused", model=model)
        async with application.run_test(size=(100, 40)) as pilot:
            await pilot.pause()

            tree = application._canvas.content
            assert style_at(tree, "roles") == {"white"}
            assert style_at(tree, "agent dispatched") == {"white"}

            application.selected = "agent-main"
            application._enter_drill()
            await pilot.pause()
            detail = application._drillbody.content
            assert style_at(detail, "update") == {"white"}
            assert style_at(detail, "result") == {"white"}
            assert style_at(detail, "finished work") == {"white"}

    asyncio.run(exercise())


def test_tree_role_legend_folds_inside_canvas_frame():
    async def exercise():
        model = FakeModel()
        labels = [
            "researcher", "designer", "coder", "tester", "reviewer",
            "debugger", "docs writer", "coordinator",
        ]
        model.nodes = {
            "main": Node("main", "main", kind="main", children=labels),
            **{
                label: Node(label, label, parent="main")
                for label in labels
            },
        }
        application = QuakePro("unused", model=model)
        async with application.run_test(size=(36, 30)) as pilot:
            await pilot.pause()

            available = max(application._canvaswrap.size.width - 4, 1)
            lines = application._canvas.content.plain.splitlines()
            legend_height = application._view["main"].row
            assert legend_height > 1
            assert all(
                cell_len(line) <= available - 2
                for line in lines[:legend_height]
            )
            assert int(application._canvas.styles.width.value) <= available

    asyncio.run(exercise())


def test_pilot_switches_labeled_view_and_preserves_tree_state():
    async def check(application, pilot):
        await pilot.press("down", "down")
        application._collapse("team:red")
        application.render_canvas()
        await pilot.press("enter", "right")
        state = (
            application._tabs.active,
            application.selected,
            set(application.collapsed),
            application.zone,
            application.drill_open,
            application.drill_sel,
            set(application.drill_expanded),
        )

        await pilot.press("v")

        assert application.view_mode == "timeline"
        assert "[Timeline · timeline]" in application.sub_title
        assert "Timeline ·" in application._canvas.content.plain
        assert application._drill.styles.display == "none"
        assert application._input.styles.display == "none"
        assert (
            application._tabs.active,
            application.selected,
            set(application.collapsed),
            application.zone,
            application.drill_open,
            application.drill_sel,
            set(application.drill_expanded),
        ) == state

        await pilot.press(
            "up", "down", "left", "right", "shift+up", "shift+down",
            "enter", "c", "a", "e", "escape",
        )

        assert (
            application._tabs.active,
            application.selected,
            set(application.collapsed),
            application.zone,
            application.drill_open,
            application.drill_sel,
            set(application.drill_expanded),
        ) == state

        await pilot.press("v")

        assert application.view_mode == "tree"
        assert "[Tree · drill]" in application.sub_title
        assert "main agent" in application._canvas.content.plain
        assert (
            application._tabs.active,
            application.selected,
            set(application.collapsed),
            application.zone,
            application.drill_open,
            application.drill_sel,
            set(application.drill_expanded),
        ) == state

    run_pilot(check)


def test_story_cache_reuses_projection_across_redraw_width_and_views(monkeypatch):
    async def exercise():
        model = FakeModel()
        model.visible_revision = 0
        calls = []
        project = app_module.SessionStoryProjector.project

        def counted(self, model, root_id):
            calls.append(root_id)
            return project(self, model, root_id)

        monkeypatch.setattr(app_module.SessionStoryProjector, "project", counted)
        application = QuakePro("unused", model=model)
        async with application.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            await pilot.press("v")
            assert calls == ["main"]

            application.render_canvas()
            await pilot.resize_terminal(80, 40)
            await pilot.pause()
            await pilot.press("v")

            assert application.view_mode == "tree"
            assert calls == ["main"]

    asyncio.run(exercise())


def test_story_cache_visible_revision_change_invalidates_once(monkeypatch):
    async def exercise():
        model = FakeModel()
        model.visible_revision = 0
        calls = []
        project = app_module.SessionStoryProjector.project

        def counted(self, model, root_id):
            calls.append(root_id)
            return project(self, model, root_id)

        monkeypatch.setattr(app_module.SessionStoryProjector, "project", counted)
        application = QuakePro("unused", model=model)
        async with application.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            await pilot.press("v")
            model.visible_revision += 1

            application.render_canvas()
            application.render_canvas()
            await pilot.press("v")

            assert application.view_mode == "tree"
            assert calls == ["main", "main"]

    asyncio.run(exercise())


def test_story_cache_reload_revision_fallback_and_loose_fake(monkeypatch):
    incremental_project = app_module.SessionStoryProjector.project
    direct_calls = []
    incremental_calls = []

    def counted_direct(model, root_id):
        direct_calls.append(model)
        return app_module.SessionStory()

    def counted_incremental(self, model, root_id):
        incremental_calls.append(model)
        return incremental_project(self, model, root_id)

    monkeypatch.setattr(app_module, "project_session_story", counted_direct)
    monkeypatch.setattr(
        app_module.SessionStoryProjector, "project", counted_incremental,
    )
    tracked = FakeModel()
    tracked._provider_reload_revision = 0
    tracked_app = QuakePro("unused", model=tracked)

    tracked_app._session_story("main")
    tracked_app._session_story("main")
    tracked._provider_reload_revision += 1
    tracked_app._session_story("main")
    tracked_app._session_story("main")

    loose = FakeModel()
    loose_app = QuakePro("unused", model=loose)
    loose_app._session_story("main")
    loose_app._session_story("main")

    assert incremental_calls == [tracked, tracked]
    assert direct_calls == [loose, loose]


def test_timeline_view_uses_canvas_width_and_live_activity_without_losing_state():
    async def exercise():
        model = FakeModel()
        model.nodes = {
            "main": Node(
                "main", "main", kind="main", children=["live", "anchor"],
                steps=[
                    Step("spawn", "Agent: live", body="Build ticket 8", child="live", ts=10),
                    Step("spawn", "Agent: anchor", body="Build ticket 9", child="anchor", ts=10),
                ],
            ),
            "live": Node(
                "live", "live", parent="main", phase="implement",
                status="running", last_ts=20,
            ),
            "anchor": Node(
                "anchor", "anchor", parent="main", phase="implement",
                status="done", last_ts=100,
            ),
        }
        observation = FakeObservation(model)
        application = QuakePro("unused", model=model, observation=observation)
        async with application.run_test(size=(72, 36)) as pilot:
            await pilot.pause()
            await pilot.press("down")
            tree_state = (
                application.selected,
                application.zone,
                set(application.collapsed),
            )
            await pilot.press("v")

            assert application.view_mode == "timeline"
            assert "[Timeline · timeline]" in application.sub_title
            before = application._canvas.content.plain
            assert max(cell_len(line) for line in before.splitlines()) <= (
                application._canvaswrap.size.width)

            model.nodes["live"].last_ts = 80
            await observation.events.put(frozenset({"session.jsonl"}))
            await pilot.pause()

            assert application.view_mode == "timeline"
            assert application._canvas.content.plain != before
            assert (
                application.selected,
                application.zone,
                set(application.collapsed),
            ) == tree_state

    asyncio.run(exercise())


def test_timeline_view_honors_canvas_width_below_twenty_cells():
    async def exercise():
        model = FakeModel()
        model.nodes = {
            "main": Node(
                "main", "main", kind="main", children=["first", "second"],
                steps=[
                    Step(
                        "spawn", "Agent: first", body="Build ticket 8",
                        child="first", ts=10,
                    ),
                    Step(
                        "spawn", "Agent: second", body="Build ticket 9",
                        child="second", ts=20,
                    ),
                ],
            ),
            "first": Node(
                "first", "coder", parent="main", status="done", last_ts=15,
            ),
            "second": Node(
                "second", "builder", parent="main", status="running", last_ts=25,
            ),
        }
        application = QuakePro("unused", model=model)
        async with application.run_test(size=(18, 30)) as pilot:
            await pilot.pause()
            await pilot.press("v")

            available = max(application._canvaswrap.size.width - 4, 1)
            lines = application._canvas.content.plain.splitlines()
            assert application.view_mode == "timeline"
            assert lines == ["T 2", "1 ⌨ coder", "│", "2 ⌨ builder", "▶"]
            assert max(cell_len(line) for line in lines) <= available
            assert int(application._canvas.styles.width.value) <= available

    asyncio.run(exercise())


def test_pilot_message_cancel_does_not_reach_next_recipient():
    async def check(application, pilot):
        application._tabs.active = _tabid("team:red")
        await pilot.pause()
        await pilot.press("down", "down", "i")
        assert application.selected == "mate-one"
        assert application._input.has_focus
        application._input.value = "draft for one"

        await pilot.press("escape")
        assert application._input.value == ""
        assert application._message_recipient is None
        await pilot.press("escape", "down", "i")
        assert application.selected == "mate-two"
        assert application._input.has_focus
        assert application._input.value == ""
        assert application._message_recipient == "mate-two"

    run_pilot(check)


def test_app_uses_injected_messenger_and_shows_send_feedback():
    async def exercise():
        messenger = MemoryMessenger()
        application = QuakePro("unused", model=FakeModel(), messenger=messenger)
        async with application.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            application._tabs.active = _tabid("team:red")
            await pilot.pause()
            await pilot.press("down", "down", "i")
            application._input.value = "status update"
            await pilot.press("enter")

            assert messenger.sent == [
                (TeammateTarget("red", "one"), "status update")
            ]
            assert "✓ sent to one" in application._drillinstr.content.plain

    asyncio.run(exercise())


def test_app_shows_messenger_failure_and_does_not_retry():
    async def exercise():
        messenger = MemoryMessenger(
            SendResult(False, "one", "inbox changed; recovery kept")
        )
        application = QuakePro("unused", model=FakeModel(), messenger=messenger)
        async with application.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            application._tabs.active = _tabid("team:red")
            await pilot.pause()
            await pilot.press("down", "down", "i")
            application._input.value = "status update"
            await pilot.press("enter")

            assert messenger.sent == [
                (TeammateTarget("red", "one"), "status update")
            ]
            assert "✗ inbox changed; recovery kept" in application._drillinstr.content.plain

    asyncio.run(exercise())


def test_app_constructs_default_messenger_once_but_not_for_injected_one(monkeypatch):
    made = []
    default = MemoryMessenger()

    def factory():
        made.append(True)
        return default

    monkeypatch.setattr(app_module, "default_messenger", factory)
    automatic = QuakePro("unused", model=FakeModel())
    supplied = MemoryMessenger()
    injected = QuakePro("unused", model=FakeModel(), messenger=supplied)

    assert made == [True]
    assert automatic.messenger is default
    assert injected.messenger is supplied


def test_pilot_switching_tabs_closes_and_resets_drill():
    async def check(application, pilot):
        await pilot.press("down", "down", "enter", "right")
        assert application.zone == "drill"
        assert application.drill_expanded == {-1}

        await pilot.press("shift+right")
        await pilot.pause()
        assert application.active_root == "wf:a-b"
        assert application.selected == "wf:a-b"
        assert application.zone == "graph"
        assert not application.drill_open
        assert application.drill_expanded == set()
        assert application.drill_sel == 0

    run_pilot(check)


def test_next_active_wraps_visible_tree():
    async def check(application, pilot):
        application.model.nodes["main"].status = "done"
        application.model.nodes["agent-main"].status = "done"
        application.model.nodes["wf:a-b"].status = "running"
        application.model.nodes["wf:a_b"].status = "done"
        application.model.nodes["team:red"].status = "error"
        application.model.nodes["mate-one"].status = "running"
        application.render_canvas()

        await pilot.press("down", "a")
        assert application.selected == "wf:a-b"
        await pilot.press("a")
        assert application.selected == "team:red"
        await pilot.press("a")
        assert application.selected == "wf:a-b"

        application.model.nodes["wf:a-b"].status = "done"
        application.model.nodes["team:red"].status = "done"
        await pilot.press("a")
        assert application.selected == "wf:a-b"

    run_pilot(check)


def test_toggle_all_branches_collapses_and_expands_current_tab():
    async def check(application, pilot):
        application._tabs.active = _tabid("team:red")
        await pilot.pause()
        await pilot.press("down", "c")
        assert application._order == ["team:red"]

        await pilot.press("c")
        assert application._order == ["team:red", "mate-one", "mate-two"]

    run_pilot(check)


def test_codex_dispatch_default_visibility_follows_agent_state():
    async def check(application, pilot):
        assert "dispatch:done" in application.collapsed
        assert "dispatch:active" not in application.collapsed
        assert "dispatch:error" not in application.collapsed
        assert application._order == [
            "main",
            "dispatch:done",
            "dispatch:active",
            "dispatch:active:0",
            "dispatch:active:1",
            "dispatch:active:2",
            "dispatch:active:3",
            "dispatch:error",
            "dispatch:error:0",
            "dispatch:error:1",
            "dispatch:error:2",
            "dispatch:error:3",
        ]

    run_dispatch_pilot(check)


def test_codex_dispatch_manual_choice_survives_live_updates():
    async def exercise():
        model = FakeModel()
        model.provider = "codex"
        model.nodes = {
            "main": Node(
                "main", "Codex main", kind="main",
                children=["first", "collapse", "open"],
            ),
            "first": Node("first", "first", parent="main"),
            "collapse": Node(
                "collapse", "dispatch 2", parent="main", kind="dispatch",
                children=["collapse:0", "collapse:1"],
            ),
            "collapse:0": Node("collapse:0", "c0", parent="collapse"),
            "collapse:1": Node("collapse:1", "c1", parent="collapse"),
            "open": Node(
                "open", "dispatch 3", parent="main", kind="dispatch",
                children=["open:0", "open:1"],
            ),
            "open:0": Node("open:0", "o0", parent="open"),
            "open:1": Node("open:1", "o1", parent="open"),
        }
        observation = FakeObservation(model)
        application = QuakePro("unused", model=model, observation=observation)
        async with application.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            await pilot.press("down", "down")
            assert application.selected == "first"

            model.nodes["live"] = Node(
                "live", "dispatch 1", parent="main", kind="dispatch", status="done",
                children=["first", "second", "live:2", "live:3"],
            )
            model.nodes["first"].status = "done"
            model.nodes["second"] = Node(
                "second", "second", parent="live", status="done",
            )
            model.nodes["live:2"] = Node(
                "live:2", "third", parent="live", status="done",
            )
            model.nodes["live:3"] = Node(
                "live:3", "fourth", parent="live", status="done",
            )
            model.nodes["first"].parent = "live"
            model.nodes["main"].children[0] = "live"
            await observation.events.put(frozenset({"rollout.jsonl"}))
            await pilot.pause()
            assert application.selected == "first"
            assert "first" in application._order

            await pilot.press("up", "left", "down", "left", "down", "right")
            assert application.selected == "open"
            assert {"live", "collapse"} <= application.collapsed
            assert "open" not in application.collapsed

            model.nodes["live"].status = "error"
            model.nodes["collapse"].status = "error"
            model.nodes["open"].status = "done"
            for child_id in ("open:0", "open:1"):
                model.nodes[child_id].status = "done"
            for index in (2, 3):
                child_id = f"open:{index}"
                model.nodes[child_id] = Node(
                    child_id, f"o{index}", parent="open", status="done",
                )
                model.nodes["open"].children.append(child_id)
            await observation.events.put(frozenset({"rollout.jsonl"}))
            await pilot.pause()

            assert {"live", "collapse"} <= application.collapsed
            assert "open" not in application.collapsed
            assert application._order[-4:] == [
                "open:0", "open:1", "open:2", "open:3",
            ]

    asyncio.run(exercise())


def test_codex_dispatch_rows_and_children_remain_navigable():
    model = FakeModel()
    model.provider = "codex"
    model.nodes = {
        "main": Node(
            "main", "Codex main", kind="main", status="done",
            children=["batch"],
        ),
        "batch": Node(
            "batch", "dispatch 1", parent="main", kind="dispatch",
            children=["finished", "active"],
        ),
        "finished": Node(
            "finished", "finished", parent="batch", status="done",
            steps=[Step("text", "text: finished", body="finished work")],
        ),
        "active": Node(
            "active", "active", parent="batch", status="running",
            steps=[Step("text", "text: working", body="active work")],
        ),
    }

    async def check(application, pilot):
        assert await pilot.click("#canvas", offset=(3, 3))
        await pilot.pause()
        assert application.zone == "graph"
        assert application.selected == "batch"

        await pilot.press("enter")
        assert application.zone == "graph"
        assert not application.drill_open
        assert application._order == ["main", "batch"]
        await pilot.press("enter")
        assert application._order == ["main", "batch", "finished", "active"]

        await pilot.press("c")
        assert application._order == ["main"]
        await pilot.press("c")
        assert application._order == ["main", "batch", "finished", "active"]

        await pilot.press("a")
        assert application.selected == "active"
        await pilot.press("enter")
        assert application.zone == "drill"
        assert application.drill_open
        assert "text: working" in application._drillbody.content.plain

    run_dispatch_pilot(check, model)


def test_tabs_have_collision_free_ids_and_existing_labels_refresh():
    async def check(application, pilot):
        first_id = _tabid("wf:a-b")
        second_id = _tabid("wf:a_b")
        assert first_id != second_id
        assert application.tab_root[first_id] == "wf:a-b"
        assert application.tab_root[second_id] == "wf:a_b"

        application.model.nodes["wf:a-b"].label = "⚙ renamed workflow"
        application.sync_tabs()
        await pilot.pause()
        assert application._tabs.get_tab(first_id).label_text == "renamed workflow"
        assert application._tabs.get_tab(second_id).label_text == "second workflow"

    run_pilot(check)


def test_tab_labels_are_literal_inline_text_and_stale_roots_are_removed():
    async def check(application, pilot):
        hostile = "[bold]literal[/bold]\nnext\x1b]0;owned\x07 [broken"
        application.model.nodes["wf:a-b"].label = "⚙ " + hostile
        application.sync_tabs()
        await pilot.pause()
        tab_id = _tabid("wf:a-b")
        expected = app_module.sanitize_inline_text(hostile)
        assert application._tabs.get_tab(tab_id).label_text == expected
        assert "[bold]" in application._tabs.get_tab(tab_id).label_text
        assert "\x1b" not in application._tabs.get_tab(tab_id).label_text

        application._tabs.active = tab_id
        await pilot.pause()
        del application.model.nodes["wf:a-b"]
        application.sync_tabs()
        await pilot.pause()
        assert tab_id not in application.tab_root
        assert application._tabs.get_tab(tab_id) is None
        assert application.active_root == "main"
        assert application.selected == "main"

    run_pilot(check)


def test_skill_overlay_starts_hidden_and_s_toggles():
    async def check(application, pilot):
        application.model.nodes["agent-main"].skills = ["development-turn"]
        application.render_canvas()
        await pilot.pause()
        assert not application.show_skills
        assert "⟡" not in application._canvas.content.plain

        await pilot.press("s")
        assert application.show_skills
        assert "⟡ development-turn" in application._canvas.content.plain

        await pilot.press("s")
        assert "⟡" not in application._canvas.content.plain

    run_pilot(check)
