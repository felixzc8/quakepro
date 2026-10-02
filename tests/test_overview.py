from __future__ import annotations

import asyncio
import copy
from dataclasses import dataclass
import os
import subprocess

import pi_fixture
import pytest
import quakepro.sessions as sessions
from quakepro.app import QuakePro, _tabid
from quakepro.graph import render_tree
from quakepro.overview import RunOverview, git_worktrees, resolve_roots
from quakepro.session_model import Node, Step
from test_app import FakeObservation


def pi_session(agent_dir, root, lines=None, name="0001_uuid.jsonl"):
    """Write a pi session where pi itself would keep it for `root`."""
    from quakepro.pi_model import pi_session_slug

    directory = os.path.join(agent_dir, "sessions", pi_session_slug(root))
    os.makedirs(directory, exist_ok=True)
    entries = lines if lines is not None else pi_fixture.entries()
    entries = [pi_fixture.header(cwd=root)] + entries[1:]
    return pi_fixture.write_session(os.path.join(directory, name), entries)


def two_roots(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.delenv("PI_CODING_AGENT_SESSION_DIR", raising=False)
    agent_dir = tmp_path / "agent"
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(agent_dir))
    roots = [str(tmp_path / "wt-a"), str(tmp_path / "wt-b")]
    for root in roots:
        os.makedirs(root, exist_ok=True)
        pi_session(str(agent_dir), root)
    return roots


@dataclass(frozen=True)
class _DiscoveryFailure:
    error_type: str
    message: str


@dataclass(frozen=True)
class _ProviderDiscovery:
    provider: str
    path: str | None
    error: object | None


@dataclass(frozen=True)
class _RootDiscovery:
    root: str
    providers: tuple

    def __iter__(self):
        return (
            result.path
            for result in self.providers
            if result.error is None and result.path is not None
        )


def discovery_result(root, claude=None, codex=None, pi=None):
    failure_type = getattr(sessions, "DiscoveryFailure", _DiscoveryFailure)
    provider_type = getattr(sessions, "ProviderDiscovery", _ProviderDiscovery)
    root_type = getattr(sessions, "RootDiscovery", _RootDiscovery)
    providers = []
    for provider, value in (("claude", claude), ("codex", codex), ("pi", pi)):
        if isinstance(value, BaseException):
            error = failure_type(type(value).__name__, str(value))
            providers.append(provider_type(provider, None, error))
        else:
            providers.append(provider_type(provider, value, None))
    return root_type(root, tuple(providers))


class ScriptedDiscovery:
    def __init__(self, *results):
        self.results = results
        self.calls = 0

    def __call__(self, root):
        result = self.results[min(self.calls, len(self.results) - 1)]
        self.calls += 1
        return result


class FakeMember:
    def __init__(self, provider, path, polls=None, watches=None, child_label="good child"):
        self.provider = provider
        self.path = path
        self.polls = list(polls or [False])
        self.watches = list(watches or [frozenset()])
        self.poll_count = 0
        self.watch_count = 0
        self.nodes = {
            "main": Node(
                "main",
                "main loop",
                status="waiting",
                kind="main",
                children=["child"],
            ),
            "child": Node("child", child_label, parent="main", status="waiting"),
        }
        self.task_list = {}

    @staticmethod
    def _outcome(script, call):
        return script[min(call, len(script) - 1)]

    def poll(self):
        outcome = self._outcome(self.polls, self.poll_count)
        self.poll_count += 1
        if isinstance(outcome, BaseException):
            raise outcome
        if callable(outcome):
            return outcome(self)
        return outcome

    def observation_paths(self):
        outcome = self._outcome(self.watches, self.watch_count)
        self.watch_count += 1
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def accepts_observation(self, changed_paths):
        return True

    def lifecycle_identity(self):
        return None


def node_with_label(model, label):
    return next(node for node in model.nodes.values() if node.label == label)


def root_child_labels(model, root_id="root:0"):
    return [model.nodes[node_id].label for node_id in model.nodes[root_id].children]


def test_overview_groups_sessions_by_root(tmp_path, monkeypatch):
    roots = two_roots(tmp_path, monkeypatch)
    model = RunOverview(roots)
    model.poll()
    assert model.nodes["main"].children == ["root:0", "root:1"]
    assert model.nodes["root:0"].label == "wt-a"
    assert model.nodes["root:1"].label == "wt-b"
    heads = [model.nodes[c].children for c in ("root:0", "root:1")]
    assert heads == [["s0:main"], ["s1:main"]]
    assert model.nodes["s0:main"].label.startswith("pi ")
    _, _, order, _ = render_tree(model, "main")
    assert order == ["main", "root:0", "s0:main", "root:1", "s1:main"]


def test_overview_keeps_each_session_actions():
    root = "/run/root-a"
    path = "/sessions/session.jsonl"
    member = FakeMember("pi", path)
    member.nodes["main"].steps = [
        Step(kind, kind) for kind in ("prompt", "thinking", "text", "tool", "text")
    ]
    model = RunOverview(
        [root],
        discover=ScriptedDiscovery(discovery_result(root, pi=path)),
        open_session=lambda opened: member,
    )
    model.poll()
    steps = node_with_label(model, "pi session").steps
    assert [step.kind for step in steps] == ["prompt", "thinking", "text", "tool", "text"]
    member.nodes["main"].steps.append(steps[0])
    assert len(node_with_label(model, "pi session").steps) == 6


def test_overview_refreshes_mirror_in_place_and_advances_visible_revision(
        monkeypatch):
    root = "/run/root-a"
    path = "/sessions/session.jsonl"

    def append_public_state(member):
        member.nodes["child"].label = "updated child"
        member.nodes["child"].steps.append(Step("text", "new step"))
        member.nodes["main"].children.append("late")
        member.nodes["late"] = Node(
            "late", "late child", parent="main", status="waiting",
        )
        return True

    member = FakeMember("pi", path, polls=[False, append_public_state, False])
    model = RunOverview(
        [root],
        discover=ScriptedDiscovery(discovery_result(root, pi=path)),
        open_session=lambda opened: member,
    )
    assert model.visible_revision == 0
    assert model.poll() is True
    revision = model.visible_revision
    nodes = model.nodes
    main = model.nodes["main"]
    root_node = model.nodes["root:0"]
    child = model.nodes["s0:child"]

    def reject_deepcopy(*_args, **_kwargs):
        raise AssertionError("overview copied full graph")

    monkeypatch.setattr(copy, "deepcopy", reject_deepcopy)
    assert model.poll() is True
    assert model.visible_revision == revision + 1
    assert model.nodes is nodes
    assert model.nodes["main"] is main
    assert model.nodes["root:0"] is root_node
    assert model.nodes["s0:child"] is child
    assert child.label == "updated child"
    assert [step.title for step in child.steps] == ["new step"]
    assert model.nodes["s0:late"].label == "late child"

    assert model.poll() is False
    assert model.visible_revision == revision + 1


def test_overview_watch_path_change_does_not_advance_visible_revision():
    root = "/run/root-a"
    path = "/sessions/session.jsonl"
    member = FakeMember(
        "pi",
        path,
        polls=[False, False],
        watches=[frozenset({"/watch/old"}), frozenset({"/watch/new"})],
    )
    model = RunOverview(
        [root],
        discover=ScriptedDiscovery(discovery_result(root, pi=path)),
        open_session=lambda opened: member,
    )
    assert model.poll() is True
    revision = model.visible_revision

    assert model.poll() is True
    assert model.visible_revision == revision
    assert "/watch/new" in model.observation_paths()
    assert "/watch/old" not in model.observation_paths()


def test_overview_skips_discovery_while_known_transcript_change_settles():
    root = "/run/root-a"
    path = "/sessions/session.jsonl"
    discovery = ScriptedDiscovery(discovery_result(root, pi=path))
    member = FakeMember("pi", path, polls=[False, True, False])
    model = RunOverview(
        [root], discover=discovery, open_session=lambda opened: member,
    )
    assert model.poll() is True
    assert discovery.calls == 1

    assert model.accepts_observation(frozenset({path})) is True
    assert model.poll() is True
    assert model.poll() is False
    assert discovery.calls == 1

    unknown = "/sessions/new-session.jsonl"
    assert model.accepts_observation(frozenset({unknown})) is True
    model.poll()
    assert discovery.calls == 2


def test_overview_shows_workflow_phase():
    root = "/run/root-a"
    path = "/sessions/session.jsonl"

    def enter_implementation(member):
        member.nodes["main"].phase = "implement"
        member.nodes["main"].workflow = ["ticket 06-awareness → in-progress"]
        return True

    member = FakeMember("pi", path, polls=[False, enter_implementation])
    model = RunOverview(
        [root],
        discover=ScriptedDiscovery(discovery_result(root, pi=path)),
        open_session=lambda opened: member,
    )
    model.poll()
    model.poll()
    assert node_with_label(model, "pi session").phase == "implement"
    text, _, _, _ = render_tree(model, "main")
    line = next(l for l in text.plain.splitlines() if "pi " in l)
    assert "▷ implement" in line
    assert "⊞ ticket 06-awareness → in-progress" in line


def test_overview_status_rolls_up_to_the_run():
    root = "/run/root-a"
    path = "/sessions/session.jsonl"

    def report_error(member):
        member.nodes["main"].status = "error"
        return True

    member = FakeMember("pi", path, polls=[False, report_error])
    model = RunOverview(
        [root],
        discover=ScriptedDiscovery(discovery_result(root, pi=path)),
        open_session=lambda opened: member,
    )
    model.poll()
    assert model.nodes["main"].status == "waiting"
    model.poll()
    assert model.nodes["root:0"].status == "error"
    assert model.nodes["main"].status == "error"


def test_overview_attaches_a_session_that_starts_later(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.delenv("PI_CODING_AGENT_SESSION_DIR", raising=False)
    agent_dir = tmp_path / "agent"
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(agent_dir))
    roots = [str(tmp_path / "wt-a"), str(tmp_path / "wt-b")]
    for root in roots:
        os.makedirs(root)
    pi_session(str(agent_dir), roots[0])
    model = RunOverview(roots)
    model.poll()
    assert model.nodes["root:1"].children == []
    pi_session(str(agent_dir), roots[1])
    assert model.poll() is True
    assert model.nodes["root:1"].children == ["s1:main"]


def test_overview_observes_each_root_provider_directory(tmp_path, monkeypatch):
    roots = two_roots(tmp_path, monkeypatch)
    model = RunOverview(roots)
    model.poll()
    paths = model.observation_paths()
    from quakepro.pi_model import pi_session_dir

    assert pi_session_dir(roots[0]) in paths
    assert pi_session_dir(roots[1]) in paths
    assert model.accepts_observation({"/anything"}) is True
    assert model.lifecycle_identity() is None
    assert model.provider == "overview"


def test_overview_keeps_failed_provider_member_while_healthy_provider_detaches():
    root = "/run/root-a"
    claude_path = "/sessions/claude.jsonl"
    codex_path = "/sessions/codex.jsonl"
    claude = FakeMember("claude", claude_path)
    codex = FakeMember("codex", codex_path)
    discovery = ScriptedDiscovery(
        discovery_result(root, claude=claude_path, codex=codex_path),
        discovery_result(root, claude=PermissionError("claude root blocked")),
        discovery_result(root, claude=PermissionError("claude root blocked")),
        discovery_result(root, claude=claude_path),
    )
    members = {claude_path: claude, codex_path: codex}
    model = RunOverview(
        [root],
        discover=discovery,
        open_session=lambda path: members[path],
    )

    assert model.poll() is True
    assert root_child_labels(model) == ["claude claude", "codex codex"]

    assert model.poll() is True
    assert root_child_labels(model) == ["claude claude", "claude discover failed"]
    assert [
        (
            failure.key,
            failure.root,
            failure.provider,
            failure.path,
            failure.stage,
            failure.error_type,
            failure.message,
        )
        for failure in model.failures
    ] == [(
        "33ff35fe24394103",
        root,
        "claude",
        "",
        "discover",
        "PermissionError",
        "claude root blocked",
    )]
    assert model.nodes["root:0"].status == "error"
    assert model.nodes["main"].status == "error"

    failure_node_id = model.nodes["root:0"].children[-1]
    assert model.poll() is False
    assert model.nodes["root:0"].children[-1] == failure_node_id
    assert discovery.calls == 3

    assert model.poll() is True
    assert model.failures == ()
    assert root_child_labels(model) == ["claude claude"]
    assert model.nodes["main"].status == "waiting"
    assert discovery.calls == 4


def test_overview_failure_order_follows_root_order_and_discovery_runs_each_poll():
    roots = ["/run/z-root", "/run/a-root"]
    calls = []

    def discover(root):
        calls.append(root)
        return discovery_result(root, claude=OSError("discovery unavailable"))

    model = RunOverview(roots, discover=discover, open_session=lambda path: None)

    assert model.poll() is True
    assert [failure.root for failure in model.failures] == roots
    assert calls == roots

    assert model.poll() is False
    assert [failure.root for failure in model.failures] == roots
    assert calls == roots + roots


def test_overview_retries_failed_replacement_open_without_detaching_old_member():
    root = "/run/root-a"
    old_path = "/sessions/old.jsonl"
    new_path = "/sessions/new.jsonl"
    old = FakeMember("claude", old_path)
    new = FakeMember("claude", new_path)
    discovery = ScriptedDiscovery(
        discovery_result(root, claude=old_path),
        discovery_result(root, claude=new_path),
        discovery_result(root, claude=new_path),
        discovery_result(root, claude=new_path),
    )
    new_attempts = []

    def open_session(path):
        if path == old_path:
            return old
        new_attempts.append(path)
        if len(new_attempts) < 3:
            raise PermissionError("new session blocked")
        return new

    model = RunOverview([root], discover=discovery, open_session=open_session)

    assert model.poll() is True
    assert model.poll() is True
    assert root_child_labels(model) == ["claude old", "claude open failed"]
    assert [(failure.path, failure.stage) for failure in model.failures] == [
        (new_path, "open"),
    ]

    assert model.poll() is False
    assert new_attempts == [new_path, new_path]
    assert root_child_labels(model) == ["claude old", "claude open failed"]

    assert model.poll() is True
    assert new_attempts == [new_path, new_path, new_path]
    assert model.failures == ()
    assert root_child_labels(model) == ["claude new"]


def test_overview_poll_failure_keeps_last_good_tree_and_recovers():
    root = "/run/root-a"
    path = "/sessions/pi.jsonl"

    def fail_with(message):
        def fail(member):
            member.nodes["child"] = Node(
                "child", "partial child", parent="main", status="waiting"
            )
            raise OSError(message)

        return fail

    def recover(member):
        member.nodes["child"] = Node(
            "child", "recovered child", parent="main", status="waiting"
        )
        return True

    member = FakeMember(
        "pi",
        path,
        polls=[
            False,
            fail_with("poll unavailable"),
            fail_with("poll unavailable"),
            fail_with("poll still unavailable"),
            recover,
        ],
    )
    model = RunOverview(
        [root],
        discover=ScriptedDiscovery(discovery_result(root, pi=path)),
        open_session=lambda opened: member,
    )

    assert model.poll() is True
    assert node_with_label(model, "good child")

    assert model.poll() is True
    assert node_with_label(model, "good child")
    assert [(failure.key, failure.message) for failure in model.failures] == [
        ("0ee680d8b7d67260", "poll unavailable"),
    ]

    assert model.poll() is False
    assert node_with_label(model, "good child")
    assert member.poll_count == 3

    assert model.poll() is True
    assert [(failure.key, failure.message) for failure in model.failures] == [
        ("0ee680d8b7d67260", "poll still unavailable"),
    ]
    assert node_with_label(model, "good child")

    assert model.poll() is True
    assert model.failures == ()
    assert node_with_label(model, "recovered child")
    assert member.poll_count == 5


def test_overview_false_poll_recovery_keeps_mirror_from_before_failed_root_replacement():
    root = "/run/root-a"
    path = "/sessions/pi.jsonl"

    def replace_root_then_fail(member):
        member.nodes = {
            "main": Node(
                "main",
                "partial main loop",
                status="waiting",
                kind="main",
                children=["partial"],
            ),
            "partial": Node(
                "partial", "partial child", parent="main", status="waiting"
            ),
        }
        raise OSError("poll unavailable")

    member = FakeMember("pi", path, polls=[False, replace_root_then_fail, False])
    model = RunOverview(
        [root],
        discover=ScriptedDiscovery(discovery_result(root, pi=path)),
        open_session=lambda opened: member,
    )

    assert model.poll() is True
    assert node_with_label(model, "good child")

    assert model.poll() is True
    assert node_with_label(model, "good child")
    assert root_child_labels(model) == ["pi pi", "pi poll failed"]
    assert [(failure.path, failure.stage, failure.message) for failure in model.failures] == [
        (path, "poll", "poll unavailable"),
    ]

    assert model.poll() is True
    assert model.failures == ()
    assert root_child_labels(model) == ["pi pi"]
    assert node_with_label(model, "good child")
    assert not any(node.label == "partial child" for node in model.nodes.values())


def test_overview_caches_watch_paths_and_reads_member_once_per_poll(monkeypatch):
    root = "/run/root-a"
    path = "/sessions/claude.jsonl"
    discovery_path_calls = []

    def discovery_paths(discovered_root):
        discovery_path_calls.append(discovered_root)
        return frozenset({"/watch/discovery"})

    monkeypatch.setattr(
        sessions,
        "discovery_observation_paths",
        discovery_paths,
        raising=False,
    )
    member = FakeMember(
        "claude",
        path,
        watches=[
            frozenset({"/watch/old"}),
            OSError("watch unavailable"),
            OSError("watch unavailable"),
            frozenset({"/watch/new"}),
            frozenset({"/watch/new"}),
        ],
    )
    model = RunOverview(
        [root],
        discover=ScriptedDiscovery(discovery_result(root, claude=path)),
        open_session=lambda opened: member,
    )

    assert model.poll() is True
    assert member.watch_count == 1
    assert model.observation_paths() == {"/watch/discovery", "/watch/old"}
    assert model.observation_paths() == {"/watch/discovery", "/watch/old"}
    assert member.watch_count == 1
    assert discovery_path_calls == [root]

    assert model.poll() is True
    assert member.watch_count == 2
    assert [(failure.key, failure.stage) for failure in model.failures] == [
        ("5cc64c677913b053", "watch"),
    ]
    assert model.observation_paths() == {"/watch/discovery", "/watch/old"}
    assert member.watch_count == 2

    assert model.poll() is False
    assert member.watch_count == 3
    assert model.observation_paths() == {"/watch/discovery", "/watch/old"}

    assert model.poll() is True
    assert member.watch_count == 4
    assert model.failures == ()
    assert model.observation_paths() == {"/watch/discovery", "/watch/new"}

    assert model.poll() is False
    assert member.watch_count == 5


@pytest.mark.parametrize(
    "invalid_paths",
    [[], {"relative/path"}, {""}, {7}],
    ids=["not-set", "relative", "empty", "not-string"],
)
def test_overview_rejects_invalid_member_watch_paths(monkeypatch, invalid_paths):
    root = "/run/root-a"
    path = "/sessions/claude.jsonl"
    monkeypatch.setattr(
        sessions,
        "discovery_observation_paths",
        lambda discovered_root: frozenset({"/watch/discovery"}),
        raising=False,
    )
    member = FakeMember("claude", path, watches=[invalid_paths])
    model = RunOverview(
        [root],
        discover=ScriptedDiscovery(discovery_result(root, claude=path)),
        open_session=lambda opened: member,
    )

    assert model.poll() is True
    assert [(failure.path, failure.stage, failure.error_type) for failure in model.failures] == [
        (path, "watch", "TypeError"),
    ]
    assert model.observation_paths() == {"/watch/discovery"}


def test_overview_rekeys_failed_watch_on_member_replacement_and_keeps_fallback(
        monkeypatch):
    root = "/run/root-a"
    old_path = "/sessions/old.jsonl"
    new_path = "/sessions/new.jsonl"
    monkeypatch.setattr(
        sessions,
        "discovery_observation_paths",
        lambda discovered_root: frozenset({"/watch/discovery"}),
        raising=False,
    )
    old = FakeMember(
        "claude",
        old_path,
        watches=[frozenset({"/watch/old"}), OSError("same watch error")],
    )
    new = FakeMember(
        "claude",
        new_path,
        watches=[OSError("same watch error"), frozenset({"/watch/new"})],
    )
    discovery = ScriptedDiscovery(
        discovery_result(root, claude=old_path),
        discovery_result(root, claude=old_path),
        discovery_result(root, claude=new_path),
        discovery_result(root, claude=new_path),
    )
    members = {old_path: old, new_path: new}
    model = RunOverview(
        [root],
        discover=discovery,
        open_session=lambda path: members[path],
    )

    assert model.poll() is True
    assert model.poll() is True
    assert [(failure.key, failure.path) for failure in model.failures] == [
        ("d930da5c7d3d370d", old_path),
    ]
    assert model.observation_paths() == {"/watch/discovery", "/watch/old"}

    assert model.poll() is True
    assert [(failure.key, failure.path) for failure in model.failures] == [
        ("1626689ddfb03053", new_path),
    ]
    assert model.observation_paths() == {"/watch/discovery", "/watch/old"}
    assert old.watch_count == 2
    assert new.watch_count == 1

    assert model.poll() is True
    assert model.failures == ()
    assert model.observation_paths() == {"/watch/discovery", "/watch/new"}
    assert new.watch_count == 2


def test_overview_orders_failure_nodes_and_clips_visible_error(monkeypatch):
    root = "/run/root-a"
    claude_path = "/sessions/claude.jsonl"
    codex_path = "/sessions/codex.jsonl"
    pi_path = "/sessions/pi.jsonl"
    long_error_type = type("LongOpenError", (OSError,), {})
    long_message = "x" * 2010
    claude = FakeMember(
        "claude",
        claude_path,
        watches=[frozenset(), OSError("claude watch unavailable")],
    )
    pi = FakeMember("pi", pi_path, polls=[False, OSError("pi poll unavailable")])
    discovery = ScriptedDiscovery(
        discovery_result(root, claude=claude_path, pi=pi_path),
        discovery_result(
            root,
            claude=PermissionError("claude discovery unavailable"),
            codex=codex_path,
            pi=pi_path,
        ),
    )

    def open_session(path):
        if path == codex_path:
            raise long_error_type(long_message)
        return {claude_path: claude, pi_path: pi}[path]

    model = RunOverview([root], discover=discovery, open_session=open_session)
    assert model.poll() is True

    assert model.poll() is True
    assert [
        (failure.key, failure.provider, failure.stage, failure.path)
        for failure in model.failures
    ] == [
        ("33ff35fe24394103", "claude", "discover", ""),
        ("5cc64c677913b053", "claude", "watch", claude_path),
        ("8525f41d4b9eddbe", "codex", "open", codex_path),
        ("0ee680d8b7d67260", "pi", "poll", pi_path),
    ]
    assert root_child_labels(model) == [
        "claude claude",
        "pi pi",
        "claude discover failed",
        "claude watch failed",
        "codex open failed",
        "pi poll failed",
    ]
    assert node_with_label(model, "claude claude").status == "waiting"
    assert node_with_label(model, "pi pi").status == "waiting"
    assert model.nodes["root:0"].status == "error"
    assert model.nodes["main"].status == "error"

    failure = next(item for item in model.failures if item.stage == "open")
    assert failure.message == "x" * 2000
    node = model.nodes["failure:" + failure.key]
    assert (node.kind, node.status, node.label) == ("issue", "error", "codex open failed")
    assert node.detail == "LongOpenError: " + "x" * 55
    assert len(node.detail) == 70
    assert [(step.kind, step.title, step.body) for step in node.steps] == [
        ("text", "open: LongOpenError", "x" * 2000),
    ]


def test_authoritative_absence_clears_member_failures_and_cached_paths(monkeypatch):
    root = "/run/root-a"
    old_path = "/sessions/old.jsonl"
    new_path = "/sessions/new.jsonl"
    monkeypatch.setattr(
        sessions,
        "discovery_observation_paths",
        lambda discovered_root: frozenset({"/watch/discovery"}),
        raising=False,
    )
    old = FakeMember(
        "claude",
        old_path,
        polls=[False, OSError("poll unavailable")],
        watches=[frozenset({"/watch/old"}), OSError("watch unavailable")],
    )
    discovery = ScriptedDiscovery(
        discovery_result(root, claude=old_path),
        discovery_result(root, claude=new_path),
        discovery_result(root),
    )

    def open_session(path):
        if path == new_path:
            raise PermissionError("open unavailable")
        return old

    model = RunOverview([root], discover=discovery, open_session=open_session)

    assert model.poll() is True
    assert model.poll() is True
    assert [failure.stage for failure in model.failures] == ["open", "poll", "watch"]
    assert model.observation_paths() == {"/watch/discovery", "/watch/old"}

    assert model.poll() is True
    assert model.failures == ()
    assert model.nodes["root:0"].children == []
    assert model.observation_paths() == {"/watch/discovery"}


def test_overview_treats_incomplete_provider_tail_as_no_change(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.delenv("PI_CODING_AGENT_SESSION_DIR", raising=False)
    agent_dir = tmp_path / "agent"
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(agent_dir))
    root = str(tmp_path / "work")
    os.makedirs(root)
    path = pi_session(str(agent_dir), root)
    model = RunOverview([root])
    assert model.poll() is True
    before = root_child_labels(model)

    with open(path, "a", encoding="utf-8") as stream:
        stream.write('{"type":"message"')

    assert model.poll() is False
    assert root_child_labels(model) == before
    assert model.nodes["root:0"].status == "waiting"


def test_overview_resolves_worktree_roots(tmp_path):
    main = tmp_path / "repo"
    main.mkdir()
    git = ["git", "-C", str(main)]
    subprocess.run(git + ["init", "-q"], check=True)
    subprocess.run(git + ["config", "user.email", "t@example.com"], check=True)
    subprocess.run(git + ["config", "user.name", "t"], check=True)
    (main / "f").write_text("x", encoding="utf-8")
    subprocess.run(git + ["add", "f"], check=True)
    subprocess.run(git + ["commit", "-qm", "init"], check=True)
    linked = tmp_path / "wt"
    subprocess.run(git + ["worktree", "add", "-q", str(linked), "-b", "side"], check=True)
    found = git_worktrees(str(main))
    assert os.path.realpath(str(main)) in [os.path.realpath(p) for p in found]
    assert os.path.realpath(str(linked)) in [os.path.realpath(p) for p in found]
    roots = resolve_roots([str(main)], worktrees=True, cwd=str(linked))
    assert roots[0] == os.path.abspath(str(main))
    assert len(roots) == len(set(roots))


def test_resolve_roots_without_a_repository(tmp_path):
    assert git_worktrees(str(tmp_path)) == []
    assert resolve_roots([str(tmp_path)], worktrees=True, cwd=str(tmp_path)) == [
        os.path.abspath(str(tmp_path))]
    assert resolve_roots([], worktrees=False, cwd=str(tmp_path)) == []


class FakeOverview:
    """A run overview containing one root whose session holds a team."""

    provider = "overview"

    def __init__(self):
        self.nodes = {
            "main": Node("main", "run", kind="main", children=["root:0"]),
            "root:0": Node("root:0", "wt-a", parent="main", kind="root",
                           children=["s0:main"]),
            "s0:main": Node("s0:main", "claude session", parent="root:0", kind="main",
                            children=["s0:team:red"]),
            "s0:team:red": Node("s0:team:red", "red team", parent="s0:main", kind="team",
                                children=["s0:mate"]),
            "s0:mate": Node("s0:mate", "one", parent="s0:team:red", kind="teammate"),
        }
        self.task_list = {}

    def poll(self):
        return None

    def observation_paths(self):
        return {os.path.dirname(os.path.abspath(__file__))}


def run_overview_pilot(check):
    async def exercise():
        application = QuakePro("unused", model=FakeOverview())
        application.observation = FakeObservation(application.model)
        async with application.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            await check(application, pilot)

    asyncio.run(exercise())


def test_overview_has_one_tab_and_expands_in_place():
    async def check(application, pilot):
        assert list(application.tab_root) == [_tabid("main")]
        assert application._stop() == ()
        application.selected = "s0:main"
        application.render_canvas()
        assert "s0:team:red" in application._view

    run_overview_pilot(check)


def test_overview_refuses_teammate_messaging():
    async def check(application, pilot):
        assert application._messageable("s0:mate") is False

    run_overview_pilot(check)
