import json
import os

from quakepro.lifecycle import (
    LifecycleJournal,
    SessionLifecycle,
    append_fact,
    fact_from_payload,
    lifecycle_path,
)
from quakepro.session_model import LifecycleFact, LifecycleSessionModel, Node


class FakeModel:
    provider = "codex"

    def __init__(self):
        self.path = "/contract/session-1.jsonl"
        self.nodes = {"main": Node("main", "fake", kind="main")}
        self.task_list = {}
        self.states = {}

    def poll(self):
        return False

    def observation_paths(self):
        return {self.path}

    def accepts_observation(self, paths):
        return self.path in paths

    def lifecycle_identity(self):
        return "codex", "session-1"

    def apply_lifecycle(self, fact, target, status, preserve_error=False):
        if preserve_error and self.states.get(target) == "error":
            return False
        changed = self.states.get(target) != status
        self.states[target] = status
        return changed


def fact(event, agent_id=""):
    return LifecycleFact("codex", "session-1", event, agent_id=agent_id)


def test_payload_normalizes_only_supported_safe_facts():
    payload = {
        "session_id": "session-1",
        "hook_event_name": "SubagentStop",
        "agent_id": "child",
        "last_assistant_message": "done",
    }
    normalized = fact_from_payload("codex", payload)

    assert normalized is not None
    assert normalized.agent_id == "child"
    assert normalized.result == "done"
    assert fact_from_payload("codex", {**payload, "session_id": "../bad"}) is None
    assert fact_from_payload("codex", {**payload, "hook_event_name": "PreToolUse"}) is None


def test_journal_is_private_append_only_and_ignores_bad_records(tmp_path, monkeypatch):
    monkeypatch.setenv("QUAKEPRO_STATE_DIR", str(tmp_path / "state"))
    path = append_fact(fact("SubagentStart", "child"))
    append_fact(fact("SubagentStop", "child"))
    with path.open("a", encoding="utf-8") as stream:
        stream.write("not json\n")
        stream.write(json.dumps({"provider": "claude", "session_id": "session-1",
                                 "event": "SessionEnd"}) + "\n")

    journal = LifecycleJournal("codex", "session-1")
    records = journal.read()

    assert [record.event for record in records] == ["SubagentStart", "SubagentStop"]
    assert journal.read() == []
    assert path == lifecycle_path("codex", "session-1")
    assert (os.stat(path).st_mode & 0o777) == 0o600
    assert (os.stat(path.parent).st_mode & 0o777) == 0o700


def test_lifecycle_reopens_then_enforces_only_authoritative_stops():
    model = FakeModel()
    assert isinstance(model, LifecycleSessionModel)
    lifecycle = SessionLifecycle(model)

    assert lifecycle.ingest([fact("SubagentStart", "child")])
    assert model.states["child"] == "running"
    model.states["child"] = "running"
    assert lifecycle.enforce() is False

    assert lifecycle.ingest([fact("SubagentStop", "child")])
    model.states["child"] = "running"
    assert lifecycle.enforce()
    assert model.states["child"] == "done"

    assert lifecycle.ingest([fact("SubagentStart", "child")])
    model.states["child"] = "done"
    assert lifecycle.enforce() is False


def test_session_wait_end_and_resume_ordering():
    model = FakeModel()
    lifecycle = SessionLifecycle(model)

    lifecycle.ingest([fact("Stop")])
    assert model.states["main"] == "waiting"
    lifecycle.ingest([fact("SessionEnd")])
    assert lifecycle.ended
    assert model.states["main"] == "done"

    lifecycle.ingest([fact("SessionStart")])
    assert not lifecycle.ended
    assert model.states["main"] == "running"
