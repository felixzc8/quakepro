import json
from dataclasses import FrozenInstanceError

import pytest

import quakepro.claude_model as claude_model
from quakepro.lifecycle import LifecycleFact as JournalLifecycleFact
from quakepro.observation import SessionObservation
from quakepro.overview import RunOverview
from quakepro.session_cache import CachedModel
from quakepro.session_model import (
    BODY_CLIP,
    DETAIL_CLIP,
    RESULT_CLIP,
    LifecycleFact,
    LifecycleSessionModel,
    Node,
    SessionModel,
    Step,
    Task,
    clip_text,
    one_line,
    parse_timestamp,
)


class ContractFake:
    provider = "fake"

    def __init__(self, path):
        self.path = str(path)
        self.nodes = {
            "main": Node("main", "fake", kind="main", children=["child"]),
            "child": Node("child", "worker", parent="main"),
        }
        self.task_list = {
            "team": [Task("task-1", subject="Check contract")],
        }

    def poll(self):
        return False

    def observation_paths(self):
        return {self.path}

    def accepts_observation(self, paths):
        return self.path in paths

    def lifecycle_identity(self):
        return None


class LifecycleContractFake(ContractFake):
    provider = "codex"

    def lifecycle_identity(self):
        return "codex", "contract-session"

    def apply_lifecycle(self, fact, target, status, preserve_error=False):
        node = self.nodes[target]
        if preserve_error and node.status == "error":
            return False
        changed = node.status != status
        node.status = status
        return changed


class CacheFake:
    def save(self, model):
        raise AssertionError("clean cached model must not save")


def claude_adapter(tmp_path):
    path = tmp_path / "claude.jsonl"
    path.write_text("", encoding="utf-8")
    return claude_model.ClaudeModel(str(path))


def codex_adapter(tmp_path):
    session_id = "019f0000-0000-7000-8000-000000000041"
    path = tmp_path / "codex.jsonl"
    record = {
        "timestamp": "2026-08-04T12:00:00Z",
        "type": "session_meta",
        "payload": {
            "id": session_id,
            "session_id": session_id,
            "thread_source": "user",
            "source": "cli",
            "cwd": str(tmp_path),
        },
    }
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    from quakepro.codex_model import CodexModel

    return CodexModel(str(path))


def pi_adapter(tmp_path):
    path = tmp_path / "pi.jsonl"
    header = {
        "type": "session",
        "version": 3,
        "id": "pi-contract-session",
        "timestamp": "2026-08-04T12:00:00.000Z",
        "cwd": str(tmp_path),
    }
    path.write_text(json.dumps(header) + "\n", encoding="utf-8")
    from quakepro.pi_model import PiModel

    return PiModel(str(path))


def cached_adapter(tmp_path):
    return CachedModel(ContractFake(tmp_path / "cached.jsonl"), CacheFake(), restored=True)


def cached_lifecycle_adapter(tmp_path):
    raw = LifecycleContractFake(tmp_path / "cached-lifecycle.jsonl")
    return CachedModel(raw, CacheFake(), restored=True)


def overview_adapter(tmp_path):
    return RunOverview([])


def fake_adapter(tmp_path):
    return ContractFake(tmp_path / "fake.jsonl")


ADAPTERS = (
    claude_adapter,
    codex_adapter,
    pi_adapter,
    cached_adapter,
    overview_adapter,
    fake_adapter,
)


def test_shared_lifecycle_fact_is_frozen_and_clip_limits_are_stable():
    assert JournalLifecycleFact is LifecycleFact
    assert (BODY_CLIP, RESULT_CLIP, DETAIL_CLIP) == (2000, 200, 70)

    fact = LifecycleFact("codex", "session-1", "SessionEnd")
    with pytest.raises(FrozenInstanceError):
        fact.event = "SessionStart"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1970-01-01T00:00:01Z", 1.0),
        ("1970-01-01T00:00:02+09:00", 2.0),
        (None, 0.0),
        ("not-a-timestamp", 0.0),
    ],
)
def test_parse_timestamp_reads_utc_prefix_or_returns_zero(value, expected):
    assert parse_timestamp(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("\n  first line  \nsecond line", "first line"),
        ("\n\t\n", ""),
        ("  one line  ", "one line"),
    ],
)
def test_one_line_returns_first_nonempty_stripped_line(value, expected):
    assert one_line(value) == expected


def test_clip_text_normalizes_line_and_clips_with_one_ellipsis():
    assert clip_text("  short  \nignored", 5) == "short"
    assert clip_text("abcdef", 4) == "abc…"
    assert clip_text("ab  cd", 5) == "ab…"
    assert clip_text("long", 1) == "…"


def test_clip_text_rejects_nonpositive_limit():
    with pytest.raises(ValueError):
        clip_text("value", 0)


@pytest.mark.parametrize("factory", ADAPTERS, ids=lambda factory: factory.__name__)
def test_all_session_adapters_and_contract_fake_have_same_runtime_surface(tmp_path, factory):
    adapter = factory(tmp_path)

    assert isinstance(adapter, SessionModel)
    assert isinstance(adapter.provider, str) and adapter.provider
    assert isinstance(adapter.path, str) and adapter.path
    assert isinstance(adapter.nodes, dict) and "main" in adapter.nodes
    assert isinstance(adapter.task_list, dict)
    assert isinstance(adapter.poll(), bool)
    paths = adapter.observation_paths()
    assert isinstance(paths, set)
    assert isinstance(adapter.accepts_observation(frozenset(paths)), bool)
    identity = adapter.lifecycle_identity()
    assert identity is None or (
        isinstance(identity, tuple)
        and len(identity) == 2
        and all(isinstance(value, str) and value for value in identity)
    )
    for node_id, node in adapter.nodes.items():
        assert isinstance(node, Node)
        assert node.id == node_id
        assert node.parent is None or node.parent in adapter.nodes
        assert all(child in adapter.nodes for child in node.children)
        assert all(isinstance(step, Step) for step in node.steps)
    assert all(
        isinstance(task, Task)
        for tasks in adapter.task_list.values()
        for task in tasks
    )


@pytest.mark.parametrize(
    "factory",
    (claude_adapter, codex_adapter, cached_lifecycle_adapter),
)
def test_adapter_with_lifecycle_identity_has_explicit_runtime_capability(tmp_path, factory):
    adapter = factory(tmp_path)

    assert adapter.lifecycle_identity() is not None
    assert isinstance(adapter, LifecycleSessionModel)


@pytest.mark.parametrize(
    "factory",
    (pi_adapter, cached_adapter, overview_adapter, fake_adapter),
)
def test_adapter_without_lifecycle_journal_returns_no_identity(tmp_path, factory):
    assert factory(tmp_path).lifecycle_identity() is None


def test_lifecycle_contract_fake_uses_shared_fact_and_exact_apply_shape(tmp_path):
    adapter = LifecycleContractFake(tmp_path / "lifecycle.jsonl")
    fact = LifecycleFact("codex", "contract-session", "SessionEnd")

    assert isinstance(adapter, SessionModel)
    assert isinstance(adapter, LifecycleSessionModel)
    assert adapter.apply_lifecycle(fact, "main", "done", preserve_error=True)
    assert adapter.nodes["main"].status == "done"


def test_observation_rejects_nonnull_lifecycle_identity_without_capability(tmp_path):
    class BrokenLifecycleModel(ContractFake):
        def lifecycle_identity(self):
            return "codex", "broken-session"

    with pytest.raises(TypeError, match="LifecycleSessionModel"):
        SessionObservation(BrokenLifecycleModel(tmp_path / "broken.jsonl"))


def test_observation_treats_empty_lifecycle_identity_as_nonnull(tmp_path):
    class EmptyIdentityModel(LifecycleContractFake):
        def lifecycle_identity(self):
            return ()

    with pytest.raises(TypeError, match="invalid lifecycle identity"):
        SessionObservation(EmptyIdentityModel(tmp_path / "empty-identity.jsonl"))


@pytest.mark.parametrize("case", ["cached", "partial", "noncallable"])
def test_observation_rejects_incomplete_lifecycle_capability(tmp_path, case):
    class IdentityOnlyModel(ContractFake):
        def lifecycle_identity(self):
            return "codex", "incomplete-session"

    class PartialModel:
        def lifecycle_identity(self):
            return "codex", "incomplete-session"

        def apply_lifecycle(self, fact, target, status, preserve_error=False):
            return False

    class NoncallableModel(IdentityOnlyModel):
        apply_lifecycle = "not callable"

    path = tmp_path / "incomplete.jsonl"
    models = {
        "cached": CachedModel(IdentityOnlyModel(path), CacheFake(), restored=True),
        "partial": PartialModel(),
        "noncallable": NoncallableModel(path),
    }

    with pytest.raises(TypeError, match="LifecycleSessionModel"):
        SessionObservation(models[case])
