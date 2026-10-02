import asyncio
import json
from pathlib import Path
import threading
import time

import pytest
from watchfiles import Change

from quakepro.lifecycle import append_fact
from quakepro.codex_model import CodexModel
from quakepro.provider_reload import visible_revision
from quakepro.observation import (
    ObservationError,
    SessionObservation,
    _minimal_roots,
    reconcile_async,
)
from quakepro.session_model import LifecycleFact, LifecycleSessionModel, Node, SessionModel


class FakeModel:
    provider = "fake"

    def __init__(self, root: Path, polls=()):
        self.root = root
        self.path = str(root)
        self.nodes = {"main": Node("main", "fake", kind="main")}
        self.task_list = {}
        self.polls = list(polls)
        self.poll_count = 0

    def observation_paths(self):
        return {str(self.root)}

    def poll(self):
        self.poll_count += 1
        return self.polls.pop(0) if self.polls else False

    def accepts_observation(self, paths):
        return True

    def lifecycle_identity(self):
        return None


class LifecycleModel(FakeModel):
    provider = "codex"

    def __init__(self, root):
        super().__init__(root)
        self.states = {}

    def lifecycle_identity(self):
        return "codex", "session-1"

    def apply_lifecycle(self, fact, target, status, preserve_error=False):
        changed = self.states.get(target) != status
        self.states[target] = status
        return changed


def test_minimal_roots_keep_exact_files_unless_outer_directory_is_requested(tmp_path):
    nested = tmp_path / "session" / "children"
    nested.mkdir(parents=True)
    transcript = nested / "rollout.jsonl"
    transcript.write_text("", encoding="utf-8")

    assert _minimal_roots([str(transcript)]) == (str(transcript),)
    assert _minimal_roots([str(transcript), str(nested), str(tmp_path)]) == (str(tmp_path),)


def test_reconcile_drains_known_backfill_without_timer(tmp_path):
    model = FakeModel(tmp_path, polls=[True, True, False])

    assert isinstance(model, SessionModel)
    assert SessionObservation(model).reconcile() is True
    assert model.poll_count == 3


def test_reconcile_rejects_model_that_never_settles(tmp_path):
    model = FakeModel(tmp_path, polls=[True, True, True])

    with pytest.raises(ObservationError, match="did not settle"):
        SessionObservation(model, settle_limit=3).reconcile()


def test_reconcile_async_returns_result_and_propagates_failure():
    async def exercise():
        assert await reconcile_async(lambda: True) is True

        def fail():
            raise ValueError("bad reconcile")

        with pytest.raises(ValueError, match="bad reconcile"):
            await reconcile_async(fail)

    asyncio.run(exercise())


def test_changes_wait_for_watcher_and_forbid_polling_fallback(tmp_path):
    calls = []

    async def watcher(*paths, **kwargs):
        calls.append((paths, kwargs))
        yield {(Change.modified, str(tmp_path / "session.jsonl"))}

    async def exercise():
        model = FakeModel(tmp_path, polls=[True, False])
        observation = SessionObservation(model, watcher=watcher)
        event = await observation.changes().__anext__()

        assert event == frozenset({str(tmp_path / "session.jsonl")})
        assert model.poll_count == 2
        assert calls == [(
            (str(tmp_path),),
            {
                "debounce": 100,
                "step": 20,
                "rust_timeout": 86_400_000,
                "force_polling": False,
                "recursive": True,
            },
        )]

    asyncio.run(exercise())


def test_changes_reconciles_on_daemon_worker(tmp_path):
    workers = []

    class RecordingModel(FakeModel):
        def poll(self):
            workers.append(threading.current_thread())
            return super().poll()

    async def watcher(*_paths, **_kwargs):
        yield {(Change.modified, str(tmp_path / "session.jsonl"))}

    async def exercise():
        model = RecordingModel(tmp_path, polls=[True, False])
        observation = SessionObservation(model, watcher=watcher)

        assert await observation.changes().__anext__() == frozenset({
            str(tmp_path / "session.jsonl")
        })

    asyncio.run(exercise())

    assert workers
    assert all(worker.daemon for worker in workers)
    assert all(worker.name == "quakepro-reconcile" for worker in workers)


def test_changes_keeps_event_loop_free_while_model_reconciles(tmp_path):
    started = threading.Event()
    release = threading.Event()
    released_at = []

    class BlockingModel(FakeModel):
        def poll(self):
            self.poll_count += 1
            if self.poll_count == 1:
                started.set()
                release.wait(timeout=1)
                return True
            return False

    async def watcher(*_paths, **_kwargs):
        yield {(Change.modified, str(tmp_path / "session.jsonl"))}

    def release_model():
        released_at.append(time.monotonic())
        release.set()

    async def exercise():
        observation = SessionObservation(BlockingModel(tmp_path), watcher=watcher)
        timer = threading.Timer(0.15, release_model)
        timer.start()
        event = asyncio.create_task(observation.changes().__anext__())
        while not started.is_set():
            await asyncio.sleep(0)
        await asyncio.sleep(0.02)
        ticked_at = time.monotonic()
        assert await event == frozenset({str(tmp_path / "session.jsonl")})
        timer.join()
        assert ticked_at < released_at[0]

    asyncio.run(exercise())


def test_watcher_failure_is_visible(tmp_path):
    async def watcher(*paths, **kwargs):
        if False:
            yield
        raise OSError("watch failed")

    async def exercise():
        model = FakeModel(tmp_path)
        with pytest.raises(ObservationError, match="filesystem observation failed"):
            await SessionObservation(model, watcher=watcher).changes().__anext__()

    asyncio.run(exercise())


def test_reconcile_folds_hook_lifecycle_facts_after_transcript(tmp_path, monkeypatch):
    monkeypatch.setenv("QUAKEPRO_STATE_DIR", str(tmp_path / "state"))
    model = LifecycleModel(tmp_path / "session")
    assert isinstance(model, LifecycleSessionModel)
    observation = SessionObservation(model)
    append_fact(LifecycleFact("codex", "session-1", "SubagentStart", agent_id="child"))
    append_fact(LifecycleFact("codex", "session-1", "SubagentStop", agent_id="child"))

    assert observation.reconcile()
    assert model.states["child"] == "done"

    append_fact(LifecycleFact("codex", "session-1", "SessionEnd"))
    assert observation.reconcile()
    assert observation.ended
    assert model.states["main"] == "done"


def test_codex_lifecycle_change_advances_visible_revision(tmp_path):
    path = tmp_path / "sessions" / "2026" / "08" / "05" / "rollout-root.jsonl"
    path.parent.mkdir(parents=True)
    session_id = "019f0000-0000-7000-8000-000000000009"
    path.write_text(json.dumps({
        "type": "session_meta",
        "payload": {
            "id": session_id,
            "session_id": session_id,
            "thread_source": "user",
            "source": "cli",
            "cwd": str(tmp_path),
        },
    }) + "\n", encoding="utf-8")
    model = CodexModel(str(path))
    assert model.poll() is True
    before = visible_revision(model)
    fact = LifecycleFact("codex", session_id, "Stop")

    assert model.apply_lifecycle(fact, "main", "waiting") is True
    assert visible_revision(model) == before + 1
    assert model.apply_lifecycle(fact, "main", "waiting") is False
    assert visible_revision(model) == before + 1


def test_reconcile_keeps_codex_journal_after_accepted_generation_rewrite(
        tmp_path, monkeypatch):
    monkeypatch.setenv("QUAKEPRO_STATE_DIR", str(tmp_path / "state"))
    path = (
        tmp_path / "sessions" / "2026" / "08" / "05" / "rollout-root.jsonl"
    )
    path.parent.mkdir(parents=True)

    def records(session_id, text):
        return [
            {
                "type": "session_meta",
                "payload": {
                    "id": session_id,
                    "session_id": session_id,
                    "thread_source": "user",
                    "source": "cli",
                    "cwd": str(tmp_path),
                },
            },
            {
                "type": "event_msg",
                "payload": {
                    "type": "agent_message",
                    "message": text,
                    "phase": "commentary",
                },
            },
        ]

    def write(records_to_write):
        path.write_text(
            "".join(json.dumps(record) + "\n" for record in records_to_write),
            encoding="utf-8",
        )

    old_id = "019f0000-0000-7000-8000-000000000011"
    new_id = "019f0000-0000-7000-8000-000000000012"
    write(records(old_id, "old value"))
    model = CodexModel(str(path))
    observation = SessionObservation(model)
    assert observation.reconcile() is True
    assert observation.reconcile() is False

    write(records(new_id, "new value"))
    append_fact(LifecycleFact("codex", new_id, "SessionEnd"))

    assert observation.reconcile() is False
    assert model.lifecycle_identity() == ("codex", old_id)
    assert [step.body for step in model.nodes["main"].steps] == ["old value"]
    assert model.nodes["main"].status == "running"
    assert not observation.ended


def test_reconcile_keeps_waiting_fact_after_same_identity_rewrite(
        tmp_path, monkeypatch):
    monkeypatch.setenv("QUAKEPRO_STATE_DIR", str(tmp_path / "state"))
    path = tmp_path / "sessions" / "2026" / "08" / "05" / "rollout-root.jsonl"
    path.parent.mkdir(parents=True)
    session_id = "019f0000-0000-7000-8000-000000000021"

    def write(text):
        records = [
            {
                "type": "session_meta",
                "payload": {
                    "id": session_id,
                    "session_id": session_id,
                    "thread_source": "user",
                    "source": "cli",
                    "cwd": str(tmp_path),
                },
            },
            {
                "type": "event_msg",
                "payload": {
                    "type": "agent_message",
                    "message": text,
                    "phase": "commentary",
                },
            },
        ]
        path.write_text(
            "".join(json.dumps(record) + "\n" for record in records),
            encoding="utf-8",
        )

    write("old value")
    model = CodexModel(str(path))
    observation = SessionObservation(model)
    observation.reconcile()
    observation.reconcile()
    append_fact(LifecycleFact("codex", session_id, "Stop"))
    assert observation.reconcile() is True
    assert model.nodes["main"].status == "waiting"

    write("new value")
    assert observation.reconcile() is False
    assert model.lifecycle_identity() == ("codex", session_id)
    assert [step.body for step in model.nodes["main"].steps] == ["old value"]
    assert model.nodes["main"].status == "waiting"
    assert observation.reconcile() is False


def test_semantic_noop_keeps_mappings_without_lifecycle_reader_rebind(
        tmp_path, monkeypatch):
    monkeypatch.setenv("QUAKEPRO_STATE_DIR", str(tmp_path / "state"))
    path = tmp_path / "sessions" / "2026" / "08" / "05" / "rollout-root.jsonl"
    path.parent.mkdir(parents=True)
    session_id = "019f0000-0000-7000-8000-000000000031"

    def write(padding):
        records = [
            {
                "type": "session_meta",
                "payload": {
                    "id": session_id,
                    "session_id": session_id,
                    "thread_source": "user",
                    "source": "cli",
                    "cwd": str(tmp_path),
                },
            },
            {
                "type": "event_msg",
                "payload": {
                    "type": "agent_message",
                    "message": "same value",
                    "phase": "commentary",
                    "padding": padding,
                },
            },
        ]
        path.write_text(
            "".join(json.dumps(record) + "\n" for record in records),
            encoding="utf-8",
        )

    write("old padding")
    model = CodexModel(str(path))
    observation = SessionObservation(model)
    assert observation.reconcile() is True
    assert observation.reconcile() is False
    nodes = model.nodes
    task_list = model.task_list
    binds = []
    original_bind = observation._bind_lifecycle

    def track_bind(identity):
        binds.append(identity)
        original_bind(identity)

    monkeypatch.setattr(observation, "_bind_lifecycle", track_bind)
    write("new longer padding")

    assert observation.reconcile() is False
    assert model.nodes is nodes
    assert model.task_list is task_list
    assert binds == []
