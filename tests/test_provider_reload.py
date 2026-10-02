from dataclasses import dataclass
import builtins
import copy
import json
import os
import time
import tracemalloc

import pytest


JSONL_RECORD_LIMIT = 64 * 1024 * 1024


def poll_replacement(participant):
    from quakepro.provider_reload import poll_append

    return poll_append(participant)


@dataclass(frozen=True)
class PlannedCandidate:
    visible: tuple[str, ...]


class JsonlParticipant:
    def __init__(self, path):
        from quakepro.provider_reload import ReloadPlan

        self.path = os.path.abspath(path)
        self.request = ReloadPlan((self.path,))
        self.visible = ()
        self.captures = []
        self.commits = 0

    def reload_plan(self):
        return self.request

    def prepare_append(self, cohort):
        self.captures.append(cohort)
        suffix = tuple(
            record["id"] for record in cohort.records_for(self.path)
        )
        visible = suffix if cohort.action == "bootstrap" else self.visible + suffix
        return PlannedCandidate(visible)

    def apply_append(self, prepared):
        changed = prepared.visible != self.visible
        self.visible = prepared.visible
        self.commits += 1
        return changed

    def settle_idle(self):
        return False


class MultiJsonlParticipant(JsonlParticipant):
    def __init__(self, paths):
        from quakepro.provider_reload import ReloadPlan

        self.paths = tuple(os.path.abspath(path) for path in paths)
        self.path = self.paths[0]
        self.request = ReloadPlan(self.paths)
        self.visible = ()
        self.captures = []
        self.commits = 0

    def prepare_append(self, cohort):
        self.captures.append(cohort)
        suffix = tuple(
            record["id"]
            for path in self.paths
            for record in cohort.records_for(path)
        )
        visible = suffix if cohort.action == "bootstrap" else self.visible + suffix
        return PlannedCandidate(visible)


class DirectoryJsonlParticipant(MultiJsonlParticipant):
    def __init__(self, main, watched):
        from quakepro.provider_reload import DirectoryRule, ReloadPlan

        self.path = os.path.abspath(main)
        self.paths = (self.path,)
        self.request = ReloadPlan(
            (self.path,),
            (DirectoryRule(
                os.path.abspath(watched),
                lambda name, _kind: name.endswith(".jsonl"),
            ),),
        )
        self.visible = ()
        self.captures = []
        self.commits = 0

    def prepare_append(self, cohort):
        self.captures.append(cohort)
        suffix = tuple(
            record["id"]
            for source in cohort.files
            for record in cohort.records_for(source.path)
        )
        visible = suffix if cohort.action == "bootstrap" else self.visible + suffix
        return PlannedCandidate(visible)


class AppendDuringPrepareParticipant(MultiJsonlParticipant):
    def __init__(self, paths, late_path):
        super().__init__(paths)
        self.late_path = late_path
        self.appended_during_prepare = False

    def prepare_append(self, cohort):
        candidate = super().prepare_append(cohort)
        if cohort.action == "append" and not self.appended_during_prepare:
            with self.late_path.open("ab") as stream:
                stream.write(_encoded_record("late-suffix"))
            self.appended_during_prepare = True
        return candidate


class RewriteDuringPrepareParticipant(MultiJsonlParticipant):
    def __init__(self, paths, rewritten_path):
        super().__init__(paths)
        self.rewritten_path = rewritten_path
        self.rewritten_during_prepare = False

    def prepare_append(self, cohort):
        candidate = super().prepare_append(cohort)
        if cohort.action == "append" and not self.rewritten_during_prepare:
            self.rewritten_path.write_bytes(
                _encoded_record("rewritten-prefix", "x" * 256)
            )
            self.rewritten_during_prepare = True
        return candidate


def _encoded_record(record_id, padding=""):
    return json.dumps(
        {"id": record_id, "padding": padding}, separators=(",", ":"),
    ).encode("utf-8") + b"\n"


def test_append_larger_than_read_chunk_commits_once_without_stall_or_duplicate(
        tmp_path):
    path = tmp_path / "source.jsonl"
    path.write_bytes(_encoded_record("prefix"))
    participant = JsonlParticipant(path)
    assert poll_replacement(participant) is True
    assert poll_replacement(participant) is False
    prior_commits = participant.commits

    with path.open("ab") as stream:
        stream.write(_encoded_record("suffix", "x" * (96 * 1024)))

    assert poll_replacement(participant) is True
    assert participant.visible == ("prefix", "suffix")
    assert participant.commits == prior_commits + 1
    assert poll_replacement(participant) is False
    assert participant.visible == ("prefix", "suffix")


def test_append_capture_retains_and_delivers_only_suffix_records(tmp_path):
    path = tmp_path / "source.jsonl"
    path.write_bytes(b"".join(
        _encoded_record(f"prefix-{index}", "x" * 1024)
        for index in range(96)
    ))
    participant = JsonlParticipant(path)
    assert poll_replacement(participant) is True
    participant.captures.clear()

    with path.open("ab") as stream:
        stream.write(_encoded_record("suffix"))

    assert poll_replacement(participant) is True
    captured = participant.captures[-1]
    source = captured.file(str(path))
    assert source is not None
    assert [record.value["id"] for record in source.records] == ["suffix"]
    assert [record.raw for record in source.records] == [
        _encoded_record("suffix").rstrip(b"\n"),
    ]
    assert [record["id"] for record in captured.records_for(str(path))] == [
        "suffix",
    ]


def test_source_and_visible_revisions_advance_independently(tmp_path):
    from quakepro.provider_reload import source_revision, visible_revision

    path = tmp_path / "source.jsonl"
    path.write_bytes(_encoded_record("visible"))
    participant = JsonlParticipant(path)

    assert poll_replacement(participant) is True
    assert source_revision(participant) == 1
    assert visible_revision(participant) == 1

    with path.open("ab") as stream:
        stream.write(b"\n")
    assert poll_replacement(participant) is False
    assert source_revision(participant) == 2
    assert visible_revision(participant) == 1


def test_append_progress_does_not_reread_unchanged_jsonl_sources(
        tmp_path, monkeypatch):
    from quakepro import provider_reload

    changed = tmp_path / "changed.jsonl"
    unchanged = [tmp_path / f"unchanged-{index}.jsonl" for index in range(8)]
    for index, path in enumerate([changed, *unchanged]):
        path.write_bytes(_encoded_record(f"prefix-{index}", "x" * (64 * 1024)))
    participant = MultiJsonlParticipant([changed, *unchanged])
    assert poll_replacement(participant) is True

    blocked = {os.path.realpath(path) for path in unchanged}

    def open_changed_only(path, mode="r", *args, **kwargs):
        if os.path.realpath(path) in blocked:
            raise AssertionError(f"unchanged source was reread: {path}")
        return builtins.open(path, mode, *args, **kwargs)

    monkeypatch.setattr(provider_reload, "open", open_changed_only, raising=False)
    with changed.open("ab") as stream:
        stream.write(_encoded_record("suffix"))

    assert poll_replacement(participant) is True
    assert participant.visible[-1] == "suffix"


def test_new_jsonl_source_does_not_rebuild_unchanged_sources(
        tmp_path, monkeypatch):
    from quakepro import provider_reload

    main = tmp_path / "main.jsonl"
    main.write_bytes(_encoded_record("main", "x" * (64 * 1024)))
    watched = tmp_path / "children"
    watched.mkdir()
    unchanged = [watched / f"child-{index}.jsonl" for index in range(8)]
    for index, path in enumerate(unchanged):
        path.write_bytes(_encoded_record(f"child-{index}", "x" * (64 * 1024)))
    participant = DirectoryJsonlParticipant(main, watched)
    assert poll_replacement(participant) is True

    blocked = {os.path.realpath(path) for path in [main, *unchanged]}

    def open_new_only(path, mode="r", *args, **kwargs):
        if os.path.realpath(path) in blocked:
            raise AssertionError(f"unchanged source was rebuilt: {path}")
        return builtins.open(path, mode, *args, **kwargs)

    monkeypatch.setattr(provider_reload, "open", open_new_only, raising=False)
    added = watched / "child-added.jsonl"
    added.write_bytes(_encoded_record("child-added"))

    assert poll_replacement(participant) is True
    assert participant.visible[-1] == "child-added"


def test_new_jsonl_path_loads_once(tmp_path, monkeypatch):
    from quakepro import provider_reload

    main = tmp_path / "main.jsonl"
    main.write_bytes(_encoded_record("main"))
    watched = tmp_path / "children"
    watched.mkdir()
    participant = DirectoryJsonlParticipant(main, watched)
    assert poll_replacement(participant) is True
    assert poll_replacement(participant) is False

    added = watched / "child-added.jsonl"
    added.write_bytes(_encoded_record("child-added"))
    target = os.path.realpath(added)
    opens = []

    def tracked_open(path, mode="r", *args, **kwargs):
        if os.path.realpath(path) == target and "r" in mode:
            opens.append(mode)
        return builtins.open(path, mode, *args, **kwargs)

    monkeypatch.setattr(provider_reload, "open", tracked_open, raising=False)

    assert poll_replacement(participant) is True
    assert participant.visible == ("main", "child-added")
    assert poll_replacement(participant) is False
    assert opens == ["rb"]


def test_append_makes_bounded_progress_when_another_source_grows_after_capture(
        tmp_path):
    first = tmp_path / "first.jsonl"
    late = tmp_path / "late.jsonl"
    first.write_bytes(_encoded_record("first-prefix"))
    late.write_bytes(_encoded_record("late-prefix"))
    participant = AppendDuringPrepareParticipant([first, late], late)
    assert poll_replacement(participant) is True

    with first.open("ab") as stream:
        stream.write(_encoded_record("first-suffix"))

    assert poll_replacement(participant) is True
    assert participant.visible[-1] == "first-suffix"
    assert poll_replacement(participant) is True
    assert participant.visible[-1] == "late-suffix"


def test_initial_capture_makes_progress_when_source_grows_during_read(
        tmp_path, monkeypatch):
    from quakepro import provider_reload

    path = tmp_path / "active.jsonl"
    path.write_bytes(_encoded_record("captured-prefix"))
    participant = JsonlParticipant(path)
    original_open = builtins.open
    appended = False

    class GrowAfterRead:
        def __init__(self, wrapped):
            self.wrapped = wrapped

        def __enter__(self):
            self.wrapped.__enter__()
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return self.wrapped.__exit__(exc_type, exc_value, traceback)

        def readline(self, *args, **kwargs):
            nonlocal appended
            result = self.wrapped.readline(*args, **kwargs)
            if not appended:
                with original_open(path, "ab") as output:
                    output.write(_encoded_record("late-suffix"))
                appended = True
            return result

        def __getattr__(self, name):
            return getattr(self.wrapped, name)

    def grow_after_read(candidate, *args, **kwargs):
        wrapped = original_open(candidate, *args, **kwargs)
        if os.path.realpath(candidate) == os.path.realpath(path):
            return GrowAfterRead(wrapped)
        return wrapped

    monkeypatch.setattr(provider_reload, "open", grow_after_read, raising=False)

    assert poll_replacement(participant) is True
    assert participant.visible == ("captured-prefix",)
    assert poll_replacement(participant) is True
    assert participant.visible == ("captured-prefix", "late-suffix")


def test_rewrite_after_capture_keeps_captured_suffix(tmp_path):
    first = tmp_path / "first.jsonl"
    rewritten = tmp_path / "rewritten.jsonl"
    first.write_bytes(_encoded_record("first-prefix"))
    rewritten.write_bytes(_encoded_record("old-prefix"))
    participant = RewriteDuringPrepareParticipant([first, rewritten], rewritten)
    assert poll_replacement(participant) is True
    before = participant.visible

    with first.open("ab") as stream:
        stream.write(_encoded_record("first-suffix"))

    assert poll_replacement(participant) is True
    assert participant.visible == before + ("first-suffix",)
    assert poll_replacement(participant) is False


class AdmissionParticipant:
    def __init__(self, root, watched):
        from quakepro.provider_reload import DirectoryRule, ReloadPlan

        self.root = os.path.realpath(root)
        self.admit_calls = []
        self.request = ReloadPlan(
            (self.root,),
            (DirectoryRule(
                os.path.realpath(watched),
                lambda name, kind: kind == "file" and name.endswith(".jsonl"),
                admit=self.admit,
            ),),
            admission_provider="codex",
        )

    def admit(self, _context, path, proof, required):
        self.admit_calls.append(os.path.realpath(path))
        return proof.root_identity == required[self.root].root_identity

    def reload_plan(self):
        return self.request

    def prepare_append(self, cohort):
        return cohort

    def apply_append(self, _prepared):
        return False

    def settle_idle(self):
        return False


def _codex_admission_bytes(thread_id, session_id):
    return json.dumps({
        "type": "session_meta",
        "payload": {
            "id": thread_id,
            "session_id": session_id,
            "cwd": "/work/project",
            "thread_source": "user" if thread_id == session_id else "subagent",
            "source": "cli",
        },
    }, separators=(",", ":")).encode("utf-8") + b"\n"


def test_governing_root_rewrite_does_not_reopen_child_admission(tmp_path):
    from quakepro.provider_reload import source_signature

    watched = tmp_path / "sessions"
    root = watched / "root.jsonl"
    child = watched / "child.jsonl"
    root.parent.mkdir()
    root.write_bytes(_codex_admission_bytes("root", "root"))
    child.write_bytes(_codex_admission_bytes("child", "root"))
    participant = AdmissionParticipant(root, watched)

    poll_replacement(participant)
    assert participant.admit_calls == [os.path.realpath(child)]
    poll_replacement(participant)
    assert participant.admit_calls == [os.path.realpath(child)]
    prior_call_count = len(participant.admit_calls)

    raw = root.read_bytes()
    before_info = root.stat()
    before = source_signature(str(root))
    time.sleep(0.01)
    root.write_bytes(raw)
    os.utime(root, ns=(before_info.st_atime_ns, before_info.st_mtime_ns))
    after = source_signature(str(root))
    assert before[:5] == after[:5]
    assert before[5] != after[5]

    poll_replacement(participant)

    assert len(participant.admit_calls) == prior_call_count


class OpaqueParticipant(JsonlParticipant):
    def prepare_append(self, cohort):
        return {"visible": tuple(
            record["id"] for record in cohort.records_for(self.path)
        )}

    def apply_append(self, prepared):
        changed = prepared["visible"] != self.visible
        self.visible = prepared["visible"]
        self.commits += 1
        return changed


def test_real_reload_plan_accepts_opaque_candidate_from_independent_participant(
        tmp_path):
    path = tmp_path / "source.jsonl"
    path.write_bytes(_encoded_record("opaque"))
    participant = OpaqueParticipant(path)

    assert poll_replacement(participant) is True
    assert participant.visible == ("opaque",)
    assert participant.commits == 1


def test_commit_failure_propagates_without_partial_state(tmp_path):
    path = tmp_path / "source.jsonl"
    path.write_bytes(_encoded_record("candidate"))
    participant = JsonlParticipant(path)

    def fail(_prepared):
        raise ValueError("commit failed")

    participant.apply_append = fail
    with pytest.raises(ValueError, match="commit failed"):
        poll_replacement(participant)
    assert participant.visible == ()
    assert participant.commits == 0


@pytest.mark.parametrize(
    ("record_size", "expected"),
    [
        pytest.param(JSONL_RECORD_LIMIT, True, id="exact-limit-crlf"),
        pytest.param(JSONL_RECORD_LIMIT + 1, False, id="over-limit-crlf"),
    ],
)
def test_reload_engine_applies_json_record_limit_before_crlf(
        tmp_path, record_size, expected):
    path = tmp_path / "source.jsonl"
    base = json.dumps({"id": "record", "padding": ""}, separators=(",", ":")).encode()
    encoded = json.dumps({
        "id": "record",
        "padding": "x" * (record_size - len(base)),
    }, separators=(",", ":")).encode()
    assert len(encoded) == record_size
    path.write_bytes(encoded + b"\r\n")
    participant = JsonlParticipant(path)

    assert poll_replacement(participant) is expected
    assert participant.visible == (("record",) if expected else ())


def _records(provider, text, padding=""):
    if provider == "claude":
        return [{
            "type": "assistant",
            "padding": padding,
            "message": {
                "timestamp": "2026-08-05T10:00:00Z",
                "content": [{"type": "text", "text": text}],
            },
        }]
    if provider == "codex":
        return [
            {
                "type": "session_meta",
                "payload": {
                    "id": "codex-root",
                    "session_id": "codex-root",
                    "cwd": "/work/project",
                    "thread_source": "user",
                    "source": "cli",
                },
            },
            {
                "type": "event_msg",
                "timestamp": "2026-08-05T10:00:00Z",
                "payload": {
                    "type": "agent_message",
                    "message": text,
                    "phase": "commentary",
                    "padding": padding,
                },
            },
        ]
    return [
        {
            "type": "session",
            "version": 3,
            "id": "pi-root",
            "cwd": "/work/project",
            "padding": padding,
        },
        {
            "type": "message",
            "id": "message-1",
            "timestamp": "2026-08-05T10:00:00Z",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": text}],
            },
        },
    ]


def _model_and_codec(provider):
    if provider == "claude":
        from quakepro.claude_snapshot import ClaudeSnapshotCodec
        from quakepro.claude_model import ClaudeModel

        return ClaudeModel, ClaudeSnapshotCodec()
    if provider == "codex":
        from quakepro.codex_model import CodexModel
        from quakepro.codex_snapshot import CodexSnapshotCodec

        return CodexModel, CodexSnapshotCodec()
    from quakepro.pi_model import PiModel
    from quakepro.pi_snapshot import PiSnapshotCodec

    return PiModel, PiSnapshotCodec()


def _write_records(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as stream:
        for record in records:
            stream.write(json.dumps(record, separators=(",", ":")).encode("utf-8"))
            stream.write(b"\n")


def _history_records(provider, count):
    records = _records(provider, "history-0", "x" * 1024)
    for index in range(1, count):
        record = _visible_record(provider, f"history-{index}")
        record["padding"] = "x" * 1024
        if provider == "pi":
            record["id"] = f"message-{index + 1}"
        records.append(record)
    return records


def _encoded_visible_record(provider, text="later"):
    return json.dumps(
        _visible_record(provider, text), separators=(",", ":"),
    ).encode("utf-8") + b"\n"


class CountedBinaryReader:
    def __init__(self, stream, reads):
        self.stream = stream
        self.reads = reads

    def __enter__(self):
        self.stream.__enter__()
        return self

    def __exit__(self, *args):
        return self.stream.__exit__(*args)

    def read(self, *args, **kwargs):
        start = self.stream.tell()
        value = self.stream.read(*args, **kwargs)
        if value:
            self.reads.append((start, len(value)))
        return value

    def readline(self, *args, **kwargs):
        start = self.stream.tell()
        value = self.stream.readline(*args, **kwargs)
        if value:
            self.reads.append((start, len(value)))
        return value

    def __getattr__(self, name):
        return getattr(self.stream, name)


def _visible_record(provider, text="later"):
    if provider == "claude":
        record = _records(provider, text)[0]
    elif provider == "codex":
        record = _records(provider, text)[1]
        record["timestamp"] = "2026-08-05T10:00:01Z"
    else:
        record = _records(provider, text)[1]
        record["id"] = "message-2"
        record["timestamp"] = "2026-08-05T10:00:01Z"
    return record


def _append_visible_record(path, provider):
    record = _visible_record(provider)
    with path.open("ab") as stream:
        stream.write(json.dumps(record, separators=(",", ":")).encode("utf-8"))
        stream.write(b"\n")


def _append_semantic_noop(path, provider):
    record = (
        {"type": "extension"}
        if provider != "codex"
        else {"type": "extension", "payload": {}}
    )
    with path.open("ab") as stream:
        stream.write(json.dumps(record, separators=(",", ":")).encode("utf-8"))
        stream.write(b"\n")


@pytest.mark.parametrize("provider", ["codex", "pi"])
def test_admitted_append_makes_progress_when_source_grows_after_capture(
        tmp_path, monkeypatch, provider):
    path = tmp_path / provider / "session.jsonl"
    _write_records(path, _records(provider, "initial"))
    model_type, _ = _model_and_codec(provider)
    session = model_type(str(path))
    _settle(session)

    original = model_type.prepare_append
    appended = False

    def append_after_capture(self, cohort):
        nonlocal appended
        candidate = original(self, cohort)
        if self is session and cohort.action == "append" and not appended:
            record = _visible_record(provider, "late")
            if provider == "codex":
                record["timestamp"] = "2026-08-05T10:00:02Z"
            else:
                record["id"] = "message-3"
                record["timestamp"] = "2026-08-05T10:00:02Z"
            with path.open("ab") as stream:
                stream.write(json.dumps(
                    record, separators=(",", ":"),
                ).encode("utf-8"))
                stream.write(b"\n")
            appended = True
        return candidate

    monkeypatch.setattr(model_type, "prepare_append", append_after_capture)
    _append_visible_record(path, provider)

    assert session.poll() is True
    assert session.poll() is True
    assert any(
        step.body == "late"
        for node in session.nodes.values()
        for step in node.steps
    )


def _tool_record(provider, name, inputs):
    if provider == "claude":
        return {
            "type": "assistant",
            "message": {"content": [{
                "type": "tool_use",
                "id": "tool-extension",
                "name": name,
                "input": inputs,
            }]},
        }
    if provider == "codex":
        return {
            "type": "response_item",
            "timestamp": "2026-08-05T10:00:01Z",
            "payload": {
                "type": "function_call",
                "call_id": "tool-extension",
                "name": name,
                "arguments": json.dumps(inputs, separators=(",", ":")),
            },
        }
    return {
        "type": "message",
        "id": "tool-extension-message",
        "timestamp": "2026-08-05T10:00:01Z",
        "message": {
            "role": "assistant",
            "stopReason": "toolUse",
            "content": [{
                "type": "toolCall",
                "id": "tool-extension",
                "name": name,
                "arguments": inputs,
            }],
        },
    }


def _settle(session):
    while session.poll():
        pass


def _projection(session):
    return copy.deepcopy((
        session.lifecycle_identity(),
        session.nodes,
        session.task_list,
    ))


def _main_resume(snapshot, path):
    target = os.path.realpath(path)
    return next(
        claim.resume
        for claim in snapshot.sources
        if claim.kind == "file" and claim.path == target
    )


@pytest.mark.parametrize("provider", ["claude", "codex", "pi"])
def test_provider_append_cost_is_independent_of_accepted_history(
        tmp_path, monkeypatch, provider):
    from quakepro import provider_admission, provider_reload

    sessions = []
    accepted_sizes = {}
    suffixes = {}
    for label, count in (("small", 1), ("large", 256)):
        path = tmp_path / provider / label / "session.jsonl"
        _write_records(path, _history_records(provider, count))
        model_type, _ = _model_and_codec(provider)
        session = model_type(str(path))
        _settle(session)
        sessions.append((label, session, path))
        accepted_sizes[os.path.realpath(path)] = path.stat().st_size
        suffixes[label] = _encoded_visible_record(provider, f"{label}-append")

    reads = {path: [] for path in accepted_sizes}
    original_open = builtins.open

    def counted_open(candidate, *args, **kwargs):
        stream = original_open(candidate, *args, **kwargs)
        path = os.path.realpath(os.path.abspath(os.fspath(candidate)))
        mode = args[0] if args else kwargs.get("mode", "r")
        if path in reads and "r" in mode and "b" in mode:
            return CountedBinaryReader(stream, reads[path])
        return stream

    whole_projection_copies = []
    original_deepcopy = copy.deepcopy

    def tracked_deepcopy(value, memo=None):
        for label, session, _path in sessions:
            if value is session or (
                type(value) is dict and value.get("nodes") is session.nodes
            ):
                whole_projection_copies.append(label)
        if memo is None:
            return original_deepcopy(value)
        return original_deepcopy(value, memo)

    monkeypatch.setattr(provider_reload, "open", counted_open, raising=False)
    monkeypatch.setattr(provider_admission, "open", counted_open, raising=False)
    monkeypatch.setattr(copy, "deepcopy", tracked_deepcopy)

    for label, session, path in sessions:
        with path.open("ab") as stream:
            stream.write(suffixes[label])
        assert session.poll() is True

    opened_bytes = {}
    for label, _session, path in sessions:
        target = os.path.realpath(path)
        opened_bytes[label] = sum(length for _start, length in reads[target])
        assert all(
            start >= accepted_sizes[target]
            for start, _length in reads[target]
        )
        assert opened_bytes[label] == len(suffixes[label])
    assert opened_bytes["small"] == opened_bytes["large"]
    assert whole_projection_copies == []


@pytest.mark.parametrize("provider", ["claude", "codex", "pi"])
def test_provider_partial_tail_retries_only_unaccepted_bytes(
        tmp_path, monkeypatch, provider):
    from quakepro import provider_admission, provider_reload

    path = tmp_path / provider / "session.jsonl"
    _write_records(path, _records(provider, "accepted"))
    model_type, _ = _model_and_codec(provider)
    session = model_type(str(path))
    _settle(session)
    accepted_size = path.stat().st_size
    encoded = _encoded_visible_record(provider)
    split = len(encoded) // 2
    reads = []
    original_open = builtins.open
    target = os.path.realpath(path)

    def counted_open(candidate, *args, **kwargs):
        stream = original_open(candidate, *args, **kwargs)
        candidate_path = os.path.realpath(os.path.abspath(os.fspath(candidate)))
        mode = args[0] if args else kwargs.get("mode", "r")
        if candidate_path == target and "r" in mode and "b" in mode:
            return CountedBinaryReader(stream, reads)
        return stream

    monkeypatch.setattr(provider_reload, "open", counted_open, raising=False)
    monkeypatch.setattr(provider_admission, "open", counted_open, raising=False)

    with path.open("ab") as stream:
        stream.write(encoded[:split])
    assert session.poll() is False
    assert all(start >= accepted_size for start, _length in reads)
    assert sum(length for _start, length in reads) == split

    reads.clear()
    with path.open("ab") as stream:
        stream.write(encoded[split:])
    assert session.poll() is True
    assert all(start >= accepted_size for start, _length in reads)
    assert sum(length for _start, length in reads) == len(encoded)
    assert [step.body for step in session.nodes["main"].steps][-1] == "later"


@pytest.mark.parametrize("provider", ["claude", "codex", "pi"])
@pytest.mark.parametrize("generation", ["truncated", "replaced"])
def test_provider_nonextending_source_generation_freezes(
        tmp_path, monkeypatch, provider, generation):
    from quakepro import provider_admission, provider_reload

    path = tmp_path / provider / "session.jsonl"
    _write_records(path, _records(provider, "accepted", "x" * 8192))
    model_type, _ = _model_and_codec(provider)
    session = model_type(str(path))
    _settle(session)
    expected = _projection(session)

    if generation == "truncated":
        path.write_bytes(b"")
    else:
        replacement = path.with_suffix(".replacement")
        _write_records(replacement, _records(provider, "replacement"))
        os.replace(replacement, path)

    target = os.path.realpath(path)
    opened = []

    def tracked_open(candidate, *args, **kwargs):
        candidate_path = os.path.realpath(os.path.abspath(os.fspath(candidate)))
        mode = args[0] if args else kwargs.get("mode", "r")
        if candidate_path == target and "r" in mode:
            opened.append(mode)
        return builtins.open(candidate, *args, **kwargs)

    monkeypatch.setattr(provider_reload, "open", tracked_open, raising=False)
    monkeypatch.setattr(provider_admission, "open", tracked_open, raising=False)

    assert session.poll() is False
    if generation == "truncated":
        _write_records(path, _records(provider, "late replacement"))
    else:
        with path.open("ab") as stream:
            stream.write(_encoded_visible_record(provider, "late replacement"))
    assert session.poll() is False
    assert _projection(session) == expected
    assert opened == []


@pytest.mark.parametrize("provider", ["claude", "codex", "pi"])
def test_provider_source_parsing_uses_engine_owned_io(
        tmp_path, monkeypatch, provider):
    import quakepro.provider_reload as provider_reload

    path = tmp_path / provider / "session.jsonl"
    _write_records(path, _records(provider, "engine input"))
    target = os.path.realpath(path)
    reads = []

    def tracked_open(candidate, *args, **kwargs):
        mode = args[0] if args else kwargs.get("mode", "r")
        if os.path.realpath(os.path.abspath(os.fspath(candidate))) == target and "b" in mode:
            reads.append(mode)
        return builtins.open(candidate, *args, **kwargs)

    def provider_local_open(*_args, **_kwargs):
        raise AssertionError("provider-local source read bypassed reload engine")

    monkeypatch.setattr(provider_reload, "open", tracked_open, raising=False)
    if provider == "claude":
        import quakepro.claude_model as provider_module
    elif provider == "codex":
        import quakepro.session_identity as provider_module
    else:
        import quakepro.pi_model as provider_module
    monkeypatch.setattr(provider_module, "open", provider_local_open, raising=False)
    model_type, _ = _model_and_codec(provider)
    session = model_type(str(path))

    assert session.poll() is True
    assert reads


@pytest.mark.parametrize("provider", ["claude", "codex", "pi"])
def test_fresh_malformed_jsonl_rejects_without_admission_and_recovers(
        tmp_path, provider):
    path = tmp_path / provider / "session.jsonl"
    records = _records(provider, "must stay private")
    _write_records(path, records)
    with path.open("ab") as stream:
        stream.write(b"{not json}\n")
    model_type, _ = _model_and_codec(provider)
    session = model_type(str(path))

    assert session.poll() is False
    assert session.nodes["main"].steps == []
    assert session.poll() is False
    assert session.nodes["main"].steps == []

    _write_records(path, _records(provider, "recovered"))
    assert session.poll() is True
    assert [step.body for step in session.nodes["main"].steps] == ["recovered"]
    assert session.poll() is False


@pytest.mark.parametrize("provider", ["claude", "codex", "pi"])
@pytest.mark.parametrize(
    ("tool_name", "accepted"),
    [
        pytest.param("provider_extension", True, id="unknown-tool"),
        pytest.param("read", False, id="known-tool"),
    ],
)
def test_recognizer_input_types_are_tool_specific(
        tmp_path, provider, tool_name, accepted):
    path = tmp_path / provider / "session.jsonl"
    _write_records(path, _records(provider, "old"))
    model_type, _ = _model_and_codec(provider)
    session = model_type(str(path))
    _settle(session)
    expected = _projection(session)

    provider_tool_name = "Read" if provider == "claude" and tool_name == "read" else tool_name
    with path.open("ab") as stream:
        stream.write(json.dumps(
            _tool_record(provider, provider_tool_name, {"path": 7}),
            separators=(",", ":"),
        ).encode("utf-8") + b"\n")

    assert session.poll() is accepted
    if accepted:
        assert any(step.tid == "tool-extension" for step in session.nodes["main"].steps)
    else:
        assert _projection(session) == expected


@pytest.mark.parametrize("provider", ["claude", "codex", "pi"])
def test_provider_participates_in_public_reload_engine(tmp_path, provider):
    path = tmp_path / provider / "session.jsonl"
    _write_records(path, _records(provider, "initial"))
    model_type, _ = _model_and_codec(provider)
    session = model_type(str(path))
    nodes = session.nodes
    main = session.nodes["main"]
    task_list = session.task_list

    assert poll_replacement(session) is True
    assert poll_replacement(session) is False
    assert session.nodes is nodes
    assert session.nodes["main"] is main
    assert session.task_list is task_list
    assert any(
        step.body == "initial"
        for step in session.nodes["main"].steps
    )


@pytest.mark.parametrize("provider", ["claude", "codex", "pi"])
def test_semantic_noop_commits_source_resume_but_reports_no_visible_change(
        tmp_path, provider):
    path = tmp_path / provider / "session.jsonl"
    _write_records(path, _records(provider, "same", "old padding"))
    model_type, codec = _model_and_codec(provider)
    session = model_type(str(path))
    _settle(session)
    expected = _projection(session)
    nodes = session.nodes
    task_list = session.task_list

    _append_semantic_noop(path, provider)

    assert session.poll() is False
    assert _projection(session) == expected
    assert session.nodes is nodes
    assert session.task_list is task_list
    snapshot = codec.capture(session)
    assert _main_resume(snapshot, str(path)).offset == path.stat().st_size
    assert session.poll() is False

    _append_visible_record(path, provider)
    assert session.poll() is True
    assert session.poll() is False


@pytest.mark.parametrize("provider", ["claude", "codex", "pi"])
def test_same_inode_rewrite_with_incomplete_tail_freezes_generation(
        tmp_path, provider):
    path = tmp_path / provider / "session.jsonl"
    _write_records(path, _records(provider, "old"))
    model_type, _ = _model_and_codec(provider)
    session = model_type(str(path))
    _settle(session)
    expected = _projection(session)
    inode = path.stat().st_ino

    records = _records(provider, "new", "replacement padding" * 32)
    tail = json.dumps(
        _visible_record(provider, "tail"), separators=(",", ":"),
    ).encode("utf-8")
    _write_records(path, records)
    with path.open("ab") as stream:
        stream.write(tail[:len(tail) // 2])
    assert path.stat().st_ino == inode

    assert session.poll() is False
    assert _projection(session) == expected
    assert session.poll() is False
    assert _projection(session) == expected

    with path.open("ab") as stream:
        stream.write(tail[len(tail) // 2:] + b"\n")
    assert session.poll() is False
    assert _projection(session) == expected
    assert session.poll() is False


@pytest.mark.parametrize("provider", ["claude", "codex", "pi"])
def test_partial_append_waits_for_record_completion(tmp_path, provider):
    path = tmp_path / provider / "session.jsonl"
    _write_records(path, _records(provider, "old"))
    model_type, _ = _model_and_codec(provider)
    session = model_type(str(path))
    _settle(session)
    expected = _projection(session)
    encoded = json.dumps(
        _visible_record(provider), separators=(",", ":"),
    ).encode("utf-8")

    with path.open("ab") as stream:
        stream.write(encoded[:len(encoded) // 2])
    assert session.poll() is False
    assert _projection(session) == expected

    with path.open("ab") as stream:
        stream.write(encoded[len(encoded) // 2:] + b"\n")
    assert session.poll() is True
    assert [step.body for step in session.nodes["main"].steps] == ["old", "later"]
    assert session.poll() is False


@pytest.mark.parametrize("provider", ["claude", "pi"])
def test_idle_large_provider_graph_has_codex_style_memory_bound(tmp_path, provider):
    path = tmp_path / provider / "session.jsonl"
    step_count = 50_000
    records = _records(provider, "step 0")
    records.extend(
        _visible_record(provider, f"step {index}")
        for index in range(1, step_count)
    )
    _write_records(path, records)
    model_type, _ = _model_and_codec(provider)
    session = model_type(str(path))
    _settle(session)
    nodes = session.nodes
    assert len(session.nodes["main"].steps) == step_count

    tracemalloc.start()
    try:
        changed = session.poll()
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert changed is False
    assert peak < 8 * 1024 * 1024
    assert session.nodes is nodes


def _oversized(record):
    value = copy.deepcopy(record)
    value["extension"] = ""
    base = json.dumps(value, separators=(",", ":")).encode("utf-8")
    value["extension"] = "x" * (JSONL_RECORD_LIMIT + 1 - len(base))
    encoded = json.dumps(value, separators=(",", ":")).encode("utf-8")
    assert len(encoded) == JSONL_RECORD_LIMIT + 1
    return value


@pytest.mark.parametrize("provider", ["claude", "codex", "pi"])
def test_oversized_rewrite_freezes_last_good_projection(
        tmp_path, provider):
    path = tmp_path / provider / "session.jsonl"
    _write_records(path, _records(provider, "old"))
    model_type, _ = _model_and_codec(provider)
    session = model_type(str(path))
    _settle(session)
    expected = _projection(session)

    invalid = _records(provider, "partial new")
    invalid[-1] = _oversized(invalid[-1])
    _write_records(path, invalid)

    assert session.poll() is False
    assert _projection(session) == expected
    assert session.poll() is False
    assert _projection(session) == expected

    _write_records(path, _records(provider, "recovered"))
    assert session.poll() is False
    assert _projection(session) == expected
    assert session.poll() is False
