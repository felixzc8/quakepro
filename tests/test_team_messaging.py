from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
import errno
import itertools
import json
import os
import threading

import pytest

from quakepro.session_model import Node
import quakepro.team_messaging as team_messaging_module
from quakepro.team_messaging import (
    ClaudeInboxMessenger,
    PosixInboxFileOps,
    SendResult,
    TeammateTarget,
    default_messenger,
)


def signature(number):
    return (1, number, number, number, bytes([number]))


class FakeModel:
    def __init__(self, provider="claude"):
        self.provider = provider
        self.nodes = {
            "main": Node("main", "main", kind="main", children=["team:red", "agent"]),
            "team:red": Node(
                "team:red",
                "red",
                parent="main",
                kind="team",
                children=["mate", "agent-under-team"],
            ),
            "mate": Node("mate", " Ada ", parent="team:red", kind="teammate"),
            "agent-under-team": Node(
                "agent-under-team", "agent", parent="team:red", kind="agent"
            ),
            "agent": Node("agent", "agent", parent="main", kind="agent"),
        }
        self.task_list = {}


class MemoryInboxFileOps:
    def __init__(self):
        self.values = {}
        self.signatures = {}
        self.calls = []
        self.next_signature = 1
        self.before_exchange = None
        self.after_exchange = None
        self.rollback_safe = True
        self.recoveries = []
        self._locks = {}
        self._locks_guard = threading.Lock()

    def _put(self, path, values):
        self.values[path] = list(values)
        self.signatures[path] = signature(self.next_signature)
        self.next_signature += 1

    @contextmanager
    def locked(self, lock_path):
        self.calls.append(("locked", lock_path))
        with self._locks_guard:
            lock = self._locks.setdefault(lock_path, threading.Lock())
        with lock:
            yield

    def ensure_list(self, path):
        self.calls.append(("ensure_list", path))
        if path not in self.values:
            self._put(path, [])

    def read(self, path):
        self.calls.append(("read", path))
        return list(self.values[path]), self.signatures[path]

    def write_temp(self, path, values):
        temporary = path + f".temp-{self.next_signature}"
        self.calls.append(("write_temp", path, list(values), temporary))
        self._put(temporary, values)
        return temporary

    def exchange(self, left, right):
        self.calls.append(("exchange", left, right))
        if self.before_exchange is not None:
            callback, self.before_exchange = self.before_exchange, None
            callback(self, right)
        self.values[left], self.values[right] = self.values[right], self.values[left]
        self.signatures[left], self.signatures[right] = (
            self.signatures[right],
            self.signatures[left],
        )
        if self.after_exchange is not None:
            callback, self.after_exchange = self.after_exchange, None
            callback(self, right)

    def rollback(self, temporary, path, desired):
        self.calls.append(("rollback", temporary, path, desired))
        if not self.rollback_safe:
            return False, "rollback conflict; recovery kept"
        if self.signatures[path] != desired:
            return False, "rollback destination changed; recovery kept"
        self.exchange(temporary, path)
        return True, ""

    def remove(self, path):
        self.calls.append(("remove", path))
        self.values.pop(path, None)
        self.signatures.pop(path, None)

    def sync_directory(self, path):
        self.calls.append(("sync_directory", path))


def messenger(files=None, *, ids=None, waits=None):
    files = files or MemoryInboxFileOps()
    ids = iter(ids or ["fixed-id"])
    waits = waits if waits is not None else []
    return ClaudeInboxMessenger(
        teams_root="/teams",
        clock=lambda: datetime(2026, 8, 7, 12, 34, 56, 789, tzinfo=timezone.utc),
        new_id=lambda: next(ids),
        files=files,
        wait=waits.append,
    )


def test_target_for_accepts_only_real_teammates_in_writable_sessions():
    sender = messenger()
    model = FakeModel()

    assert sender.target_for(model, "mate") == TeammateTarget("red", "Ada")
    assert sender.target_for(model, "mate#0") is None
    assert sender.target_for(model, "agent-under-team") is None
    assert sender.target_for(model, "agent") is None
    assert sender.target_for(model, "missing") is None

    model.provider = "overview"
    assert sender.target_for(model, "mate") is None


def test_target_for_rejects_transcript_path_escape_components():
    sender = messenger()

    for team, name in [
        ("..", "Ada"),
        ("/outside", "Ada"),
        ("red/../../outside", "Ada"),
        ("red", "../outside"),
        ("red", "/outside"),
        ("red", r"..\outside"),
    ]:
        model = FakeModel()
        model.nodes["team:red"].id = f"team:{team}"
        model.nodes["mate"].label = name

        assert sender.target_for(model, "mate") is None


def test_blank_send_fails_before_clock_id_or_file_access():
    files = MemoryInboxFileOps()
    clock_calls = []
    id_calls = []
    sender = ClaudeInboxMessenger(
        teams_root="/teams",
        clock=lambda: clock_calls.append(True),
        new_id=lambda: id_calls.append(True),
        files=files,
        wait=lambda delay: None,
    )

    result = sender.send(TeammateTarget("red", "Ada"), " \n ")

    assert not result.ok
    assert result.recipient == "Ada"
    assert "blank" in result.detail
    assert clock_calls == []
    assert id_calls == []
    assert files.calls == []


def test_send_rejects_path_escape_components_before_file_access():
    files = MemoryInboxFileOps()
    sender = messenger(files)

    for target in [
        TeammateTarget("..", "Ada"),
        TeammateTarget("/outside", "Ada"),
        TeammateTarget("red/../../outside", "Ada"),
        TeammateTarget("red", "../outside"),
        TeammateTarget("red", "/outside"),
        TeammateTarget("red", r"..\outside"),
    ]:
        result = sender.send(target, "do not write")

        assert result == SendResult(False, target.name, "invalid teammate target")

    assert files.calls == []


def test_send_uses_injected_identity_and_clock_and_commits_current_schema():
    files = MemoryInboxFileOps()
    sender = messenger(files)
    text = "x" * 65

    result = sender.send(TeammateTarget("red", "Ada"), text)

    assert result == SendResult(True, "Ada")
    path = "/teams/red/inboxes/Ada.json"
    assert files.values[path] == [
        {
            "id": "quakepro-fixed-id",
            "from": "team-lead",
            "text": text,
            "summary": "x" * 60,
            "timestamp": "2026-08-07T12:34:56.000789Z",
            "type": "message",
            "read": False,
        }
    ]
    assert files.calls[0] == ("locked", path + ".lock")
    assert ("ensure_list", path) in files.calls
    assert ("sync_directory", "/teams/red/inboxes") in files.calls
    assert any(call[0] == "remove" for call in files.calls)
    assert not any(".temp-" in name for name in files.values)


def test_concurrent_sends_preserve_every_message_under_one_inbox_lock():
    files = MemoryInboxFileOps()
    next_id = itertools.count()
    sender = ClaudeInboxMessenger(
        teams_root="/teams",
        clock=lambda: datetime(2026, 8, 7, tzinfo=timezone.utc),
        new_id=lambda: f"id-{next(next_id)}",
        files=files,
        wait=lambda delay: None,
    )
    target = TeammateTarget("red", "Ada")
    barrier = threading.Barrier(12)

    def send(index):
        barrier.wait()
        return sender.send(target, f"message {index}")

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(send, range(12)))

    assert all(result == SendResult(True, "Ada") for result in results)
    inbox = files.values["/teams/red/inboxes/Ada.json"]
    assert {entry["text"] for entry in inbox} == {
        f"message {index}" for index in range(12)
    }
    assert len({entry["id"] for entry in inbox}) == 12
    assert not any(".temp-" in name for name in files.values)


def test_real_filesystem_concurrent_sends_preserve_every_message(tmp_path):
    files = PosixInboxFileOps()
    left = tmp_path / "exchange-left"
    right = tmp_path / "exchange-right"
    left.write_text("left", encoding="utf-8")
    right.write_text("right", encoding="utf-8")
    try:
        files.exchange(str(left), str(right))
    except OSError as exc:
        unsupported = {
            errno.ENOSYS,
            errno.ENOTSUP,
            errno.EOPNOTSUPP,
            errno.EINVAL,
        }
        if exc.errno in unsupported:
            pytest.skip(f"atomic exchange unavailable: {exc}")
        raise
    assert left.read_text(encoding="utf-8") == "right"
    assert right.read_text(encoding="utf-8") == "left"

    target = TeammateTarget("red", "Ada")
    barrier = threading.Barrier(12)

    def send(index):
        sender = ClaudeInboxMessenger(
            teams_root=str(tmp_path),
            clock=lambda: datetime(2026, 8, 7, tzinfo=timezone.utc),
            new_id=lambda: f"id-{index}",
            files=PosixInboxFileOps(),
            wait=lambda delay: None,
        )
        barrier.wait(timeout=10)
        return sender.send(target, f"message {index}")

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(send, range(12)))

    assert results == [SendResult(True, "Ada")] * 12
    inbox_path = tmp_path / "red" / "inboxes" / "Ada.json"
    inbox = json.loads(inbox_path.read_text(encoding="utf-8"))
    assert {entry["id"] for entry in inbox} == {
        f"quakepro-id-{index}" for index in range(12)
    }
    assert {entry["text"] for entry in inbox} == {
        f"message {index}" for index in range(12)
    }
    assert len(inbox) == 12
    assert sorted(path.name for path in inbox_path.parent.iterdir()) == [
        "Ada.json",
        "Ada.json.lock",
    ]


@pytest.mark.parametrize("component", ["teams", "team", "inboxes", "inbox", "lock"])
def test_send_rejects_symlink_paths_without_touching_target(tmp_path, component):
    teams = tmp_path / "teams"
    inboxes = teams / "red" / "inboxes"
    inboxes.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "target.json"
    target.write_text("[]")
    original = target.stat()
    if component == "teams":
        inboxes.rmdir()
        inboxes.parent.rmdir()
        teams.rmdir()
        teams.symlink_to(outside, target_is_directory=True)
    elif component == "team":
        inboxes.rmdir()
        inboxes.parent.rmdir()
        inboxes.parent.symlink_to(outside, target_is_directory=True)
    elif component == "inboxes":
        inboxes.rmdir()
        inboxes.symlink_to(outside, target_is_directory=True)
    else:
        filename = "Ada.json.lock" if component == "lock" else "Ada.json"
        (inboxes / filename).symlink_to(target)
    sender = ClaudeInboxMessenger(
        str(teams), lambda: datetime.now(timezone.utc), lambda: "synthetic",
        PosixInboxFileOps(), lambda _: None,
    )

    result = sender.send(TeammateTarget("red", "Ada"), "synthetic message")

    assert not result.ok
    assert sorted(path.name for path in outside.iterdir()) == ["target.json"]
    assert target.read_text() == "[]"
    assert target.stat().st_mode == original.st_mode
    assert target.stat().st_mtime_ns == original.st_mtime_ns


@pytest.mark.parametrize("filename", ["Ada.json", "Ada.json.lock"])
def test_send_rejects_hardlinked_inbox_and_lock(tmp_path, filename):
    inboxes = tmp_path / "red" / "inboxes"
    inboxes.mkdir(parents=True)
    target = tmp_path / "outside.json"
    target.write_text("[]")
    os.link(target, inboxes / filename)
    sender = ClaudeInboxMessenger(
        str(tmp_path), lambda: datetime.now(timezone.utc), lambda: "synthetic",
        PosixInboxFileOps(), lambda _: None,
    )

    assert not sender.send(TeammateTarget("red", "Ada"), "synthetic message").ok
    assert target.read_text() == "[]"


def test_send_keeps_admitted_directory_when_parent_is_replaced(tmp_path):
    teams = tmp_path / "teams"
    (teams / "red" / "inboxes").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    parked = tmp_path / "original-team"

    class SwapParent(PosixInboxFileOps):
        @contextmanager
        def inbox(self, teams_root, team):
            with super().inbox(teams_root, team) as admitted:
                (teams / "red").rename(parked)
                (teams / "red").symlink_to(outside, target_is_directory=True)
                yield admitted

    sender = ClaudeInboxMessenger(
        str(teams), lambda: datetime.now(timezone.utc), lambda: "synthetic",
        SwapParent(), lambda _: None,
    )

    assert sender.send(TeammateTarget("red", "Ada"), "synthetic message").ok
    assert not list(outside.iterdir())
    inbox = json.loads((parked / "inboxes" / "Ada.json").read_text())
    assert [item["text"] for item in inbox] == ["synthetic message"]


def test_concurrent_exchange_preserves_external_read_and_deletion_on_retry():
    files = MemoryInboxFileOps()
    path = "/teams/red/inboxes/Ada.json"
    unread = {"id": "old", "text": "old", "read": False}
    deleted = {"id": "deleted", "text": "remove", "read": False}
    files._put(path, [unread, deleted])

    def external_change(operations, inbox_path):
        operations._put(inbox_path, [{**unread, "read": True}])

    files.before_exchange = external_change
    waits = []
    sender = messenger(files, waits=waits)

    result = sender.send(TeammateTarget("red", "Ada"), "new")

    assert result == SendResult(True, "Ada")
    assert waits == [0.01]
    assert files.values[path][0] == {**unread, "read": True}
    assert not any(entry.get("id") == "deleted" for entry in files.values[path])
    sent = [entry for entry in files.values[path] if entry.get("id") == "quakepro-fixed-id"]
    assert len(sent) == 1
    assert sum(call[0] == "rollback" for call in files.calls) == 1
    assert sum(call[0] == "write_temp" for call in files.calls) == 2


def test_consumption_immediately_after_exchange_is_not_requeued():
    files = MemoryInboxFileOps()
    path = "/teams/red/inboxes/Ada.json"
    files._put(path, [])

    def consume(operations, inbox_path):
        operations._put(inbox_path, [])

    files.after_exchange = consume
    sender = messenger(files)

    result = sender.send(TeammateTarget("red", "Ada"), "consume once")

    assert result == SendResult(True, "Ada")
    assert files.values[path] == []
    assert sum(call[0] == "write_temp" for call in files.calls) == 1
    assert sum(call[0] == "rollback" for call in files.calls) == 0
    assert files.recoveries == []


def test_error_after_clean_rollback_leaves_external_inbox_authoritative():
    class SecondWriteFails(MemoryInboxFileOps):
        def __init__(self):
            super().__init__()
            self.write_count = 0

        def write_temp(self, path, values):
            self.write_count += 1
            if self.write_count == 2:
                raise OSError(errno.ENOSPC, "disk full")
            return super().write_temp(path, values)

    files = SecondWriteFails()
    path = "/teams/red/inboxes/Ada.json"
    files._put(path, [])
    external = [{"id": "external", "text": "keep me", "read": True}]

    def external_change(operations, inbox_path):
        operations._put(inbox_path, external)

    files.before_exchange = external_change
    sender = messenger(files)

    result = sender.send(TeammateTarget("red", "Ada"), "not delivered")

    assert not result.ok
    assert "disk full" in result.detail
    assert files.values[path] == external
    assert files.write_count == 2
    assert sum(call[0] == "rollback" for call in files.calls) == 1


def test_unsafe_rollback_returns_failure_and_keeps_recovery_without_retry():
    class UnsafeRecoveryInboxFileOps(MemoryInboxFileOps):
        def rollback(self, temporary, path, desired):
            self.calls.append(("rollback", temporary, path, desired))
            authoritative = list(self.values[temporary])
            conflict = [{"id": "post-swap", "text": "also preserve"}]
            self._put(path, authoritative)
            self.recoveries.append(conflict)
            self.remove(temporary)
            return False, "rollback conflict; recovery kept"

    files = UnsafeRecoveryInboxFileOps()
    path = "/teams/red/inboxes/Ada.json"
    files._put(path, [])
    authoritative = [{"id": "external", "text": "authoritative"}]

    def external_change(operations, inbox_path):
        operations._put(inbox_path, authoritative)

    files.before_exchange = external_change
    waits = []
    sender = messenger(files, waits=waits)

    result = sender.send(TeammateTarget("red", "Ada"), "new")

    assert not result.ok
    assert result.recipient == "Ada"
    assert "rollback conflict" in result.detail
    assert files.values[path] == authoritative
    assert files.recoveries == [[{"id": "post-swap", "text": "also preserve"}]]
    assert waits == []
    assert sum(call[0] == "write_temp" for call in files.calls) == 1


def test_rollback_io_failure_keeps_displaced_authoritative_recovery():
    class FailedRollbackInboxFileOps(MemoryInboxFileOps):
        def rollback(self, temporary, path, desired):
            self.calls.append(("rollback", temporary, path, desired))
            self.recoveries.append(list(self.values[temporary]))
            return False, "rollback failed; recovery kept"

    files = FailedRollbackInboxFileOps()
    path = "/teams/red/inboxes/Ada.json"
    files._put(path, [])
    authoritative = [{"id": "external", "text": "recover me"}]

    def external_change(operations, inbox_path):
        operations._put(inbox_path, authoritative)

    files.before_exchange = external_change
    sender = messenger(files)

    result = sender.send(TeammateTarget("red", "Ada"), "new")

    assert not result.ok
    assert "rollback failed" in result.detail
    assert files.recoveries == [authoritative]
    assert sum(call[0] == "write_temp" for call in files.calls) == 1


def test_retry_exhaustion_uses_32_exchanges_and_linear_injected_waits():
    class AlwaysChangingInboxFileOps(MemoryInboxFileOps):
        def __init__(self):
            super().__init__()
            self.change_before_next_exchange = False

        def write_temp(self, path, values):
            temporary = super().write_temp(path, values)
            self.change_before_next_exchange = True
            return temporary

        def exchange(self, left, right):
            if self.change_before_next_exchange:
                self.change_before_next_exchange = False
                self._put(right, [{"id": f"external-{self.next_signature}"}])
            super().exchange(left, right)

    files = AlwaysChangingInboxFileOps()
    files._put("/teams/red/inboxes/Ada.json", [])
    waits = []
    sender = messenger(files, waits=waits)

    result = sender.send(TeammateTarget("red", "Ada"), "new")

    assert not result.ok
    assert "changing" in result.detail
    assert sum(call[0] == "write_temp" for call in files.calls) == 32
    assert waits == [0.01 * attempt for attempt in range(1, 32)]


def test_unsupported_atomic_exchange_fails_without_mutating_authoritative_inbox():
    class UnsupportedExchangeInboxFileOps(MemoryInboxFileOps):
        def exchange(self, left, right):
            self.calls.append(("exchange", left, right))
            raise OSError(errno.ENOTSUP, "atomic exchange unavailable")

    files = UnsupportedExchangeInboxFileOps()
    path = "/teams/red/inboxes/Ada.json"
    original = [{"from": "external", "text": "keep me"}]
    files._put(path, original)
    sender = messenger(files)

    result = sender.send(TeammateTarget("red", "Ada"), "new message")

    assert not result.ok
    assert "unavailable" in result.detail
    assert files.values[path] == original
    assert sum(call[0] == "exchange" for call in files.calls) == 1


def test_invalid_inbox_fails_before_write_and_is_not_replaced():
    class InvalidInboxFileOps(MemoryInboxFileOps):
        def read(self, path):
            self.calls.append(("read", path))
            raise ValueError("inbox JSON is not a list")

    files = InvalidInboxFileOps()
    path = "/teams/red/inboxes/Ada.json"
    invalid_source = {"unread": True}
    files.values[path] = invalid_source
    sender = messenger(files)

    result = sender.send(TeammateTarget("red", "Ada"), "do not lose data")

    assert not result.ok
    assert "not a list" in result.detail
    assert files.values[path] == invalid_source
    assert not any(call[0] == "write_temp" for call in files.calls)
    assert not any(call[0] == "exchange" for call in files.calls)


def test_default_messenger_composes_runtime_files_utc_clock_and_unique_ids(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(team_messaging_module, "TEAMS", str(tmp_path))
    sender = default_messenger()
    target = TeammateTarget("red", "Ada")

    assert sender.send(target, "same").ok
    assert sender.send(target, "same").ok

    inbox = json.loads(
        (tmp_path / "red" / "inboxes" / "Ada.json").read_text(encoding="utf-8")
    )
    assert len(inbox) == 2
    assert len({entry["id"] for entry in inbox}) == 2
    assert all(entry["id"].startswith("quakepro-") for entry in inbox)
    timestamps = [datetime.fromisoformat(entry["timestamp"].replace("Z", "+00:00"))
                  for entry in inbox]
    assert all(value.tzinfo == timezone.utc for value in timestamps)
