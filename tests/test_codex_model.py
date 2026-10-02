import builtins
import copy
import json
import os
import time
import tracemalloc
from pathlib import Path

import pytest

import quakepro.codex_model as codex_model
import quakepro.provider_reload as provider_reload
import quakepro.session_identity as session_identity
from quakepro.codex_model import CodexModel
from quakepro.codex_snapshot import CodexSnapshotCodec
from quakepro.graph import render_tree
from quakepro.lifecycle import SessionLifecycle
from quakepro.session_model import LifecycleFact


ROOT_ID = "019f0000-0000-7000-8000-000000000001"
CHILD_ID = "019f0000-0000-7000-8000-000000000002"
GRAND_ID = "019f0000-0000-7000-8000-000000000003"
GUARDIAN_ID = "019f0000-0000-7000-8000-000000000004"
REPLACEMENT_ID = "019f0000-0000-7000-8000-000000000005"


def record(record_type, payload, ts="2026-07-09T10:00:00Z"):
    return {"timestamp": ts, "type": record_type, "payload": payload}


def root_meta(thread_id=ROOT_ID, cwd="/work/project"):
    return record("session_meta", {
        "id": thread_id,
        "session_id": thread_id,
        "thread_source": "user",
        "source": "cli",
        "cwd": cwd,
    })


def child_meta(thread_id=CHILD_ID, parent=ROOT_ID, path="/root/planner",
               nickname="Curie", depth=1, session_id=ROOT_ID):
    return record("session_meta", {
        "id": thread_id,
        "session_id": session_id,
        "thread_source": "subagent",
        "parent_thread_id": parent,
        "agent_path": path,
        "agent_nickname": nickname,
        "source": {"subagent": {"thread_spawn": {
            "parent_thread_id": parent,
            "depth": depth,
            "agent_path": path,
            "agent_nickname": nickname,
        }}},
    })


def guardian_meta():
    return record("session_meta", {
        "id": GUARDIAN_ID,
        "session_id": ROOT_ID,
        "thread_source": "subagent",
        "parent_thread_id": ROOT_ID,
        "source": {"subagent": {"other": "guardian"}},
        "multi_agent_version": "disabled",
    })


def event(event_type, ts="2026-07-09T10:00:01Z", **values):
    return record("event_msg", {"type": event_type, **values}, ts)


def response(response_type, ts="2026-07-09T10:00:02Z", **values):
    return record("response_item", {"type": response_type, **values}, ts)


def write_jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(item) + "\n" for item in records))


def append_jsonl(path, records):
    with path.open("a") as fh:
        for item in records:
            fh.write(json.dumps(item) + "\n")


def rollout_path(tmp_path, thread_id, name="10-00-00"):
    return (tmp_path / "sessions" / "2026" / "07" / "09" /
            f"rollout-2026-07-09T{name}-{thread_id}.jsonl")


def public_projection(session):
    return copy.deepcopy((
        session.lifecycle_identity(),
        session.nodes,
        session.task_list,
    ))


def settle_model(session):
    while session.poll():
        pass


def rewrite_after_metadata_capture(monkeypatch, path, replacement):
    original_open = builtins.open
    target_path = os.path.realpath(path)
    rewritten = []

    class MetadataReader:
        def __init__(self, wrapped):
            self.wrapped = wrapped
            self.captured = False

        def __enter__(self):
            self.wrapped.__enter__()
            return self

        def __exit__(self, *args):
            result = self.wrapped.__exit__(*args)
            if self.captured and not rewritten:
                path.write_bytes(replacement)
                rewritten.append(True)
            return result

        def __getattr__(self, name):
            return getattr(self.wrapped, name)

        def readline(self, *args, **kwargs):
            line = self.wrapped.readline(*args, **kwargs)
            self.captured = bool(line)
            return line

    def open_then_rewrite(candidate, *args, **kwargs):
        wrapped = original_open(candidate, *args, **kwargs)
        candidate_path = os.path.realpath(os.path.abspath(os.fspath(candidate)))
        return MetadataReader(wrapped) if candidate_path == target_path else wrapped

    monkeypatch.setattr(provider_reload, "open", open_then_rewrite, raising=False)
    return rewritten


def rewrite_records(text, padding="", thread_id=ROOT_ID):
    return [
        root_meta(thread_id),
        event("agent_message", message=text, phase="commentary", padding=padding),
    ]


def _raw_record(value):
    return json.dumps(value, separators=(",", ":")).encode("utf-8") + b"\n"


def _nested_value(containers):
    value = 0
    for _ in range(containers):
        value = [value]
    return value


def codex_semantic_replacement(case):
    metadata = root_meta()
    partial = event("agent_message", message="partial new", phase="commentary")
    if case.startswith("recognizer-input-"):
        field = case.removeprefix("recognizer-input-")
        if field == "edits":
            value = {"new_string": "not a list"}
        elif field == "edits-item":
            field = "edits"
            value = [{"new_string": 7}]
        else:
            value = 7
        if field == "skill":
            name = "Skill"
        elif field in {"command", "cmd"}:
            name = "exec_command"
        elif field == "patch":
            name = "apply_patch"
        elif field in {
            "prompt", "message", "task", "description", "instructions",
            "task_name",
        }:
            name = "spawn_agent"
        else:
            name = "edit"
        invalid = response(
            "function_call",
            name=name,
            call_id="recognizer-call",
            arguments=json.dumps({field: value}),
        )
        return [metadata, partial, invalid]
    if case.startswith("session-meta-"):
        field = case.removeprefix("session-meta-").replace("-", "_")
        metadata["payload"][field] = 7
        return [metadata, partial]
    if case == "record-timestamp":
        invalid = event("agent_message", message="typed field", phase="commentary")
        invalid["timestamp"] = 7
    elif case == "token-count-output":
        invalid = event(
            "token_count",
            info={"total_token_usage": {"output_tokens": "13"}},
        )
    elif case in {"function-call-id", "custom-call-id"}:
        response_type = "function_call" if case == "function-call-id" else "custom_tool_call"
        invalid = response(response_type, name="shell", call_id=7, arguments="{}")
    elif case in {"function-output-id", "custom-output-id"}:
        response_type = (
            "function_call_output" if case == "function-output-id"
            else "custom_tool_call_output"
        )
        invalid = response(response_type, call_id=7, output="done")
    elif case == "call-name":
        invalid = response("function_call", name=7, call_id="call-1", arguments="{}")
    elif case == "call-status":
        invalid = response(
            "function_call", name="shell", call_id="call-1", arguments="{}", status=7,
        )
    elif case == "output-status":
        invalid = response(
            "function_call_output", call_id="missing-call", output="done", status=7,
        )
    elif case == "output-body":
        invalid = response("function_call_output", call_id="missing-call", output=7)
    elif case == "user-message":
        invalid = event("user_message", message=7)
    elif case == "task-result":
        invalid = event("task_complete", turn_id="turn-1", last_agent_message=7)
    elif case == "abort-reason":
        invalid = event("turn_aborted", turn_id="turn-1", reason=7)
    elif case == "reasoning-summary":
        invalid = response("reasoning", summary=7)
    elif case == "assistant-content":
        invalid = response("message", role="assistant", phase="commentary", content=7)
    elif case == "assistant-role":
        invalid = response("message", role=7, phase="commentary", content="typed field")
    elif case == "assistant-phase":
        invalid = response("message", role="assistant", phase=7, content="typed field")
    elif case == "function-call-arguments":
        invalid = response(
            "function_call", name="shell", call_id="call-1", arguments={"bad": 1},
        )
    elif case == "custom-call-input":
        invalid = response(
            "custom_tool_call", name="shell", call_id="call-1", input=["bad"],
        )
    elif case == "inter-agent-trigger-turn":
        invalid = record(
            "inter_agent_communication_metadata", {"trigger_turn": "true"},
        )
    elif case == "agent-message-recipient":
        invalid = response(
            "agent_message", author="/root", recipient=7, content="typed field",
        )
    elif case.startswith("completed-activity-"):
        invalid = completed_activity()
        field = case.removeprefix("completed-activity-").replace("-", "_")
        item_field = "id" if field == "event_id" else field
        invalid["payload"]["item"][item_field] = 7
    elif case.startswith("activity-"):
        invalid = event(
            "sub_agent_activity",
            agent_thread_id="child-thread",
            agent_path="/root/child",
            event_id="event-1",
            kind="started",
        )
        field = case.removeprefix("activity-").replace("-", "_")
        invalid["payload"][field] = 7
    return [metadata, partial, invalid]


@pytest.mark.parametrize(
    ("old_padding", "new_padding"),
    [("x" * 256, ""), ("x" * 32, "y" * 32), ("", "y" * 256)],
)
def test_codex_accepted_rewrite_freezes_projection(
        tmp_path, old_padding, new_padding):
    path = rollout_path(tmp_path, ROOT_ID)
    write_jsonl(path, rewrite_records("old value", old_padding))
    session = CodexModel(str(path))
    settle_model(session)
    expected = public_projection(session)

    write_jsonl(path, rewrite_records("new value", new_padding))

    assert session.poll() is False
    assert public_projection(session) == expected


def test_codex_noop_poll_with_many_steps_has_bounded_extra_memory(tmp_path):
    root = rollout_path(tmp_path, ROOT_ID)
    child = rollout_path(tmp_path, CHILD_ID, "10-01-00")
    step_count = 50_000
    write_jsonl(root, [
        root_meta(),
        *(
            event("agent_message", message=f"step {index}", phase="commentary")
            for index in range(step_count)
        ),
    ])
    write_jsonl(child, rewrite_child_records("child work"))
    session = CodexModel(str(root))
    settle_model(session)
    main = session.nodes["main"]
    assert len(main.steps) == step_count

    tracemalloc.start()
    try:
        changed = session.poll()
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert changed is False
    assert peak < 8 * 1024 * 1024
    assert session.nodes["main"] is main


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_codex_appended_tool_arguments_reject_nonfinite_values_before_commit(
        tmp_path, constant):
    path = rollout_path(tmp_path, ROOT_ID)
    write_jsonl(path, rewrite_records("old value"))
    session = CodexModel(str(path))
    settle_model(session)
    expected = public_projection(session)

    append_jsonl(path, [response(
        "function_call",
        name="provider_extension",
        call_id="nonfinite-tool",
        arguments='{"extension":%s}' % constant,
    )])

    assert session.poll() is False
    assert public_projection(session) == expected


@pytest.mark.parametrize(
    "case",
    [
        "session-meta-cwd",
        "session-meta-thread-source",
        "session-meta-source",
        "record-timestamp",
        "token-count-output",
        "function-call-id",
        "custom-call-id",
        "function-output-id",
        "custom-output-id",
        "call-name",
        "call-status",
        "output-status",
        "output-body",
        "user-message",
        "task-result",
        "abort-reason",
        "reasoning-summary",
        "assistant-content",
        "assistant-role",
        "assistant-phase",
        "function-call-arguments",
        "custom-call-input",
        "inter-agent-trigger-turn",
        "agent-message-recipient",
        "activity-agent-thread-id",
        "activity-agent-path",
        "activity-event-id",
        "activity-kind",
        "completed-activity-agent-thread-id",
        "completed-activity-agent-path",
        "completed-activity-event-id",
        "completed-activity-kind",
        "recognizer-input-skill",
        "recognizer-input-file_path",
        "recognizer-input-notebook_path",
        "recognizer-input-path",
        "recognizer-input-command",
        "recognizer-input-cmd",
        "recognizer-input-patch",
        "recognizer-input-new_string",
        "recognizer-input-newText",
        "recognizer-input-new_str",
        "recognizer-input-content",
        "recognizer-input-text",
        "recognizer-input-old_string",
        "recognizer-input-oldText",
        "recognizer-input-old_str",
        "recognizer-input-prompt",
        "recognizer-input-message",
        "recognizer-input-task",
        "recognizer-input-description",
        "recognizer-input-instructions",
        "recognizer-input-task_name",
        "recognizer-input-edits",
        "recognizer-input-edits-item",
    ],
)
def test_codex_appended_records_require_exact_graph_field_types(tmp_path, case):
    path = rollout_path(tmp_path, ROOT_ID)
    write_jsonl(path, rewrite_records("old value"))
    session = CodexModel(str(path))
    settle_model(session)
    expected = public_projection(session)

    append_jsonl(path, codex_semantic_replacement(case))

    assert session.poll() is False
    assert public_projection(session) == expected
    assert session.poll() is False
    assert public_projection(session) == expected


@pytest.mark.parametrize(
    "non_finite",
    [
        pytest.param(float("nan"), id="nan"),
        pytest.param(float("inf"), id="positive-infinity"),
        pytest.param(float("-inf"), id="negative-infinity"),
    ],
)
def test_codex_appended_non_finite_metadata_is_atomic(tmp_path, non_finite):
    path = rollout_path(tmp_path, ROOT_ID)
    write_jsonl(path, rewrite_records("old value"))
    session = CodexModel(str(path))
    settle_model(session)
    expected = public_projection(session)
    invalid = rewrite_records("partial new")
    invalid[0]["payload"]["future"] = {"scores": [non_finite]}

    append_jsonl(path, invalid)

    assert session.poll() is False
    assert public_projection(session) == expected


def test_codex_appended_invalid_agent_message_is_atomic(tmp_path):
    path = rollout_path(tmp_path, ROOT_ID)
    write_jsonl(path, rewrite_records("old value"))
    session = CodexModel(str(path))
    settle_model(session)
    expected = public_projection(session)

    append_jsonl(path, [
        event("agent_message", message="partial new", phase="commentary"),
        event("agent_message", message={"unexpected": "shape"}),
    ])

    assert session.poll() is False
    assert public_projection(session) == expected


def test_codex_append_during_suffix_read_is_deferred_to_next_poll(
        tmp_path, monkeypatch):
    path = rollout_path(tmp_path, ROOT_ID)
    write_jsonl(path, rewrite_records("old value"))
    session = CodexModel(str(path))
    settle_model(session)
    append_jsonl(path, [
        event("agent_message", message="new value", phase="commentary"),
    ])

    original_open = builtins.open
    appended = False
    body_reads = 0

    class AppendMalformedTail:
        def __init__(self, wrapped):
            self.wrapped = wrapped

        def __enter__(self):
            self.wrapped.__enter__()
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            nonlocal appended
            result = self.wrapped.__exit__(exc_type, exc_value, traceback)
            if not appended:
                with original_open(path, "ab") as output:
                    output.write(b"{not json}\n")
                appended = True
            return result

        def __getattr__(self, name):
            return getattr(self.wrapped, name)

    def append_after_body_read(candidate, *args, **kwargs):
        nonlocal body_reads
        wrapped = original_open(candidate, *args, **kwargs)
        mode = args[0] if args else kwargs.get("mode", "r")
        if os.path.abspath(os.fspath(candidate)) == str(path) and "b" in mode:
            body_reads += 1
            if body_reads == 1:
                return AppendMalformedTail(wrapped)
        return wrapped

    monkeypatch.setattr(provider_reload, "open", append_after_body_read, raising=False)

    assert session.poll() is True
    assert appended is True
    assert body_reads == 1
    assert [step.body for step in session.nodes["main"].steps] == [
        "old value", "new value",
    ]
    assert session.poll() is False


def test_codex_staged_append_during_read_commits_captured_prefix(
        tmp_path, monkeypatch):
    root = rollout_path(tmp_path, ROOT_ID)
    child = rollout_path(tmp_path, CHILD_ID, "10-01-00")
    write_jsonl(root, rewrite_records("old root"))
    write_jsonl(child, rewrite_child_records("old child"))
    session = CodexModel(str(root))
    settle_model(session)
    assert "cx:" + CHILD_ID in session.nodes
    append_jsonl(root, [
        event("agent_message", message="root append", phase="commentary"),
    ])
    original_open = builtins.open
    appended = False
    body_reads = 0

    class AppendMalformedTail:
        def __init__(self, wrapped):
            self.wrapped = wrapped

        def __enter__(self):
            self.wrapped.__enter__()
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            nonlocal appended
            result = self.wrapped.__exit__(exc_type, exc_value, traceback)
            if not appended:
                with original_open(root, "ab") as output:
                    output.write(b"{not json}\n")
                appended = True
            return result

        def __getattr__(self, name):
            return getattr(self.wrapped, name)

    def append_after_body_read(candidate, *args, **kwargs):
        nonlocal body_reads
        wrapped = original_open(candidate, *args, **kwargs)
        mode = args[0] if args else kwargs.get("mode", "r")
        if os.path.abspath(os.fspath(candidate)) == str(root) and "b" in mode:
            body_reads += 1
            return AppendMalformedTail(wrapped)
        return wrapped

    monkeypatch.setattr(provider_reload, "open", append_after_body_read, raising=False)

    assert session.poll() is True
    assert appended is True
    assert body_reads == 1
    assert [step.body for step in session.nodes["main"].steps] == [
        "old root", "root append",
    ]
    assert session.poll() is False
    assert [step.body for step in session.nodes["cx:" + CHILD_ID].steps] == [
        "old child",
    ]
    assert session.poll() is False


def trigger(path):
    return [
        record("inter_agent_communication_metadata", {"trigger_turn": True}),
        response("agent_message", author="/root", recipient=path,
                 content=[{"type": "input_text", "text": "Message Type: NEW_TASK"},
                          {"type": "encrypted_content", "encrypted_content": "secret"}]),
    ]


def spawn_call(call_id="call_spawn", task_name="planner"):
    return response("function_call", name="spawn_agent", namespace="collaboration",
                    call_id=call_id, arguments=json.dumps({
                        "task_name": task_name,
                        "fork_turns": "all",
                        "message": "gAAAAA-encrypted-task-body",
                    }))


def activity(thread_id=CHILD_ID, path="/root/planner", call_id="call_spawn",
             kind="started"):
    return event("sub_agent_activity", agent_thread_id=thread_id,
                 agent_path=path, event_id=call_id, kind=kind)


def test_codex_child_label_uses_spawn_task_name(tmp_path):
    root = rollout_path(tmp_path, ROOT_ID)
    write_jsonl(root, [
        root_meta(),
        spawn_call(task_name="review_parser"),
        activity(path="/root/path-leaf"),
    ])

    model = CodexModel(str(root))
    model.poll()

    assert model.nodes["cx:" + CHILD_ID].label == "review_parser"


def completed_activity(thread_id=CHILD_ID, path="/root/planner",
                       call_id="call_spawn", kind="started"):
    return event("item_completed", item={
        "type": "SubAgentActivity",
        "id": call_id,
        "agent_thread_id": thread_id,
        "agent_path": path,
        "kind": kind,
    })


def rewrite_child_records(text):
    return [
        child_meta(),
        event("task_started", turn_id="child-turn"),
        *trigger("/root/planner"),
        event("agent_message", message=text, phase="commentary"),
    ]


def test_codex_pending_candidate_retries_transient_metadata_read(tmp_path, monkeypatch):
    root = rollout_path(tmp_path, ROOT_ID)
    child = rollout_path(tmp_path, CHILD_ID, "10-01-00")
    write_jsonl(root, rewrite_records("root work"))
    session = CodexModel(str(root))
    settle_model(session)
    expected = public_projection(session)
    write_jsonl(child, rewrite_child_records("child work"))

    original_open = builtins.open
    target_path = os.path.realpath(child)
    failed = False

    def fail_child_metadata_once(candidate, *args, **kwargs):
        nonlocal failed
        candidate_path = os.path.realpath(os.path.abspath(os.fspath(candidate)))
        if not failed and candidate_path == target_path:
            failed = True
            raise OSError("transient metadata read")
        return original_open(candidate, *args, **kwargs)

    monkeypatch.setattr(
        provider_reload, "open", fail_child_metadata_once, raising=False,
    )
    assert session.poll() is False
    assert public_projection(session) == expected

    assert session.poll() is True
    assert [step.body for step in session.nodes["cx:" + CHILD_ID].steps] == [
        "child work"
    ]
    assert session.poll() is False


def test_codex_accepted_child_deletion_freezes_projection(tmp_path):
    root = rollout_path(tmp_path, ROOT_ID)
    child = rollout_path(tmp_path, CHILD_ID, "10-01-00")
    write_jsonl(root, rewrite_records("root"))
    write_jsonl(child, rewrite_child_records("child"))
    session = CodexModel(str(root))
    settle_model(session)
    expected = public_projection(session)

    child.unlink()

    assert session.poll() is False
    assert public_projection(session) == expected


@pytest.mark.parametrize(
    "case",
    [
        "id",
        "parent-thread-id",
        "agent-path",
        "agent-nickname",
        "thread-source",
        "source-subagent",
        "spawn-parent-thread-id",
        "spawn-depth",
        "spawn-agent-path",
        "spawn-agent-nickname",
    ],
)
def test_codex_new_child_metadata_requires_exact_types(tmp_path, case):
    root = rollout_path(tmp_path, ROOT_ID)
    child = rollout_path(tmp_path, CHILD_ID, "10-01-00")
    write_jsonl(root, rewrite_records("root"))
    session = CodexModel(str(root))
    settle_model(session)
    expected = public_projection(session)
    invalid_meta = child_meta()
    payload = invalid_meta["payload"]
    if case in {
        "id", "parent-thread-id", "agent-path", "agent-nickname", "thread-source",
    }:
        payload[case.replace("-", "_")] = 7
    elif case == "source-subagent":
        payload["source"]["subagent"] = ["not", "an", "object"]
    else:
        field = case.removeprefix("spawn-").replace("-", "_")
        payload["source"]["subagent"]["thread_spawn"][field] = (
            "1" if field == "depth" else 7
        )
    write_jsonl(child, [
        invalid_meta,
        event("agent_message", message="private child", phase="commentary"),
    ])

    assert session.poll() is False
    assert public_projection(session) == expected
    assert "cx:" + CHILD_ID not in session.nodes


def test_root_messages_tools_tokens_model_and_lifecycle(tmp_path):
    path = rollout_path(tmp_path, ROOT_ID)
    write_jsonl(path, [
        root_meta(),
        event("task_started", turn_id="turn-1"),
        record("turn_context", {"turn_id": "turn-1", "model": "gpt-5.6-codex"}),
        event("agent_message", message="Checking the repository", phase="commentary"),
        response("message", ts="2026-07-09T10:05:00Z", role="assistant", phase="commentary",
                 content=[{"type": "output_text", "text": "Checking the repository"}]),
        response("reasoning", summary=[{"type": "summary_text", "text": "Plan the change"}],
                 encrypted_content="ciphertext"),
        response("custom_tool_call", name="exec", call_id="call-exec",
                 input='{"cmd":"uv run pytest"}', status="completed"),
        response("custom_tool_call_output", call_id="call-exec",
                 output=[{"type": "input_text", "text": "24 passed"}]),
        event("token_count", info={"total_token_usage": {"output_tokens": 8}}),
        event("token_count", info={"total_token_usage": {"output_tokens": 13}}),
        event("agent_message", message="Implemented successfully", phase="final_answer"),
        event("task_complete", turn_id="turn-1", last_agent_message="Implemented successfully"),
        record("unknown", {"type": "future_record"}),
    ])

    model = CodexModel(str(path))
    assert model.poll() is True

    main = model.nodes["main"]
    assert model.provider == "codex"
    assert model.path == str(path)
    assert model.task_list == {}
    assert main.model == "gpt-5.6-codex"
    assert main.tokens == 13
    assert main.status == "done"
    assert main.result == "Implemented successfully"
    assert [step.kind for step in main.steps] == ["text", "thinking", "tool", "text"]
    assert main.steps[2].status == "done"
    assert main.steps[2].output == "24 passed"
    assert sum(step.body == "Checking the repository" for step in main.steps) == 1


def test_codex_lifecycle_enforcement_settles_after_long_result(tmp_path):
    path = rollout_path(tmp_path, ROOT_ID)
    write_jsonl(path, [root_meta()])
    model = CodexModel(str(path))
    lifecycle = SessionLifecycle(model)
    fact = LifecycleFact(
        "codex", ROOT_ID, "SessionEnd", result="x" * 201
    )

    assert lifecycle.ingest([fact]) is True
    assert model.nodes["main"].result == "x" * 200
    assert lifecycle.enforce() is False


def test_codex_default_lifecycle_type_uses_generic_placeholder(tmp_path):
    path = rollout_path(tmp_path, ROOT_ID)
    child = rollout_path(tmp_path, CHILD_ID, "10-01-00")
    write_jsonl(path, [root_meta()])
    model = CodexModel(str(path))
    assert model.poll() is True
    fact = LifecycleFact(
        "codex", ROOT_ID, "SubagentStart",
        agent_id=CHILD_ID,
        agent_type="default",
    )

    assert model.apply_lifecycle(fact, CHILD_ID, "running") is True
    assert model.nodes["cx:" + CHILD_ID].label == "agent"

    write_jsonl(child, [child_meta(path="/root/default", nickname="Curie")])
    assert model.poll() is True
    assert model.nodes["cx:" + CHILD_ID].label == "agent"
    assert model.nodes["cx:" + CHILD_ID].detail == "Curie"


def test_codex_user_message_is_recorded_as_a_root_turn_prompt(tmp_path):
    path = rollout_path(tmp_path, ROOT_ID)
    write_jsonl(path, [
        root_meta(),
        event("user_message", message="Build Workstreams view"),
    ])

    model = CodexModel(str(path))

    assert model.poll() is True
    assert [(step.kind, step.body) for step in model.nodes["main"].steps] == [
        ("prompt", "Build Workstreams view"),
    ]


def test_unified_exec_tool_unwraps_inner_tool_name_and_detail(tmp_path):
    path = rollout_path(tmp_path, ROOT_ID)
    write_jsonl(path, [
        root_meta(),
        response("custom_tool_call", name="exec", call_id="c1",
                 input='const r = await tools.exec_command({cmd:"uv run pytest -q",'
                       '"workdir":"/work"}); text(r.output);'),
        response("custom_tool_call_output", call_id="c1", output="24 passed"),
        response("custom_tool_call", name="exec", call_id="c2", input=(
            'const patch = "*** Begin Patch\\n*** Update File: /work/app.py\\n'
            '@@\\n-a\\n+b\\n*** End Patch";\ntext(await tools.apply_patch(patch));')),
        response("custom_tool_call", name="exec", call_id="c3",
                 input='const r = await tools.update_plan({plan:[{step:"Ship it",'
                       '"status":"in_progress"}]}); text(r)'),
        response("custom_tool_call", name="exec", call_id="c4",
                 input='const r = await tools.write_stdin({session_id:5,chars:""}); text(r)'),
    ])
    model = CodexModel(str(path))
    model.poll()

    steps = model.nodes["main"].steps
    assert [step.title for step in steps] == [
        "uv: run pytest -q",
        "apply_patch: app.py",
        "update_plan: Ship it",
        "write_stdin",
    ]
    assert steps[0].status == "done"
    assert steps[0].output == "24 passed"
    assert "*** Update File: /work/app.py" in steps[1].body   # de-escaped, readable


def test_shell_tool_titles_use_first_program_for_wrapped_and_bare_calls(tmp_path):
    path = rollout_path(tmp_path, ROOT_ID)
    write_jsonl(path, [
        root_meta(),
        response("custom_tool_call", name="exec", call_id="wrapped", input=(
            'const r = await tools.exec_command({cmd:"uv run pytest -q"}); text(r.output);')),
        response("function_call", name="exec_command", call_id="bare",
                 arguments=json.dumps({
                     "cmd": "MODE=fast /usr/bin/rg -n needle src",
                 })),
        response("custom_tool_call", name="exec", call_id="dynamic", input=(
            "const cmd = chooseCommand(); "
            "const r = await tools.exec_command({cmd}); text(r.output);")),
    ])

    model = CodexModel(str(path))
    model.poll()

    assert [step.title for step in model.nodes["main"].steps] == [
        "uv: run pytest -q",
        "rg: -n needle src",
        "shell",
    ]


def test_child_snapshot_isolation_spawn_link_and_encryption_omission(tmp_path):
    root = rollout_path(tmp_path, ROOT_ID)
    child = rollout_path(tmp_path, CHILD_ID, "10-01-00")
    write_jsonl(root, [
        root_meta(),
        event("task_started", turn_id="root-turn"),
        event("agent_message", message="Parent work", phase="commentary"),
        spawn_call(),
        activity(),
        response("function_call_output", call_id="call_spawn", output='{"task":"planner"}'),
    ])
    child_records = [
        child_meta(),
        root_meta(),
        event("task_started", turn_id="copied-root-turn"),
        record("turn_context", {"turn_id": "copied-root-turn", "model": "parent-model"}),
        event("agent_message", message="Copied parent text", phase="commentary"),
        event("token_count", info={"total_token_usage": {"output_tokens": 999}}),
        event("task_complete", turn_id="copied-root-turn", last_agent_message="copied result"),
        event("task_started", turn_id="child-turn"),
        record("turn_context", {"turn_id": "child-turn", "model": "gpt-5.6-sol"}),
        *trigger("/root/planner"),
        event("agent_message", message="Child-only work", phase="commentary"),
        event("token_count", info={"total_token_usage": {"output_tokens": 21}}),
        event("task_complete", turn_id="child-turn", last_agent_message="child result"),
    ]
    write_jsonl(child, child_records)

    model = CodexModel(str(root))
    model.poll()

    node = model.nodes["cx:" + CHILD_ID]
    spawn = next(step for step in model.nodes["main"].steps if step.kind == "spawn")
    assert node.parent == "main"
    assert node.label == "planner"
    assert node.detail == "Curie"
    assert node.model == "gpt-5.6-sol"
    assert node.tokens == 21
    assert node.status == "done"
    assert node.result == "child result"
    assert [step.body for step in node.steps] == ["Child-only work"]
    assert spawn.child == node.id
    assert spawn.status == "done"
    assert "gAAAAA" not in spawn.body
    assert "encrypted-task-body" not in spawn.body


def test_ciphertext_substrings_are_omitted_from_visible_fields(tmp_path):
    path = rollout_path(tmp_path, ROOT_ID)
    ciphertext = "gAAAAA" + "A" * 80
    write_jsonl(path, [
        root_meta(),
        event("agent_message", message=f"before {ciphertext} after", phase="commentary"),
        response("reasoning", summary=[], encrypted_content=ciphertext),
        response("custom_tool_call", name="exec", call_id="encrypted-call",
                 input=json.dumps({"command": f"before {ciphertext} after"})),
        response("custom_tool_call_output", call_id="encrypted-call",
                 output=f"before {ciphertext} after"),
    ])

    model = CodexModel(str(path))
    model.poll()
    visible = "".join(step.title + step.body + step.output for step in model.nodes["main"].steps)

    assert ciphertext not in visible
    assert "gAAAAA" not in visible
    assert visible.count("before  after") >= 3


def test_nested_agents_follow_metadata_and_close_each_spawn(tmp_path):
    root = rollout_path(tmp_path, ROOT_ID)
    child = rollout_path(tmp_path, CHILD_ID, "10-01-00")
    grand = rollout_path(tmp_path, GRAND_ID, "10-02-00")
    write_jsonl(root, [root_meta(), spawn_call(), activity()])
    write_jsonl(child, [
        child_meta(),
        event("task_started", turn_id="child-turn"),
        *trigger("/root/planner"),
        spawn_call("call-grand", "reviewer"),
        activity(GRAND_ID, "/root/planner/reviewer", "call-grand"),
        event("task_complete", turn_id="child-turn", last_agent_message="child done"),
    ])
    write_jsonl(grand, [
        child_meta(GRAND_ID, CHILD_ID, "/root/planner/reviewer", "Hume", 2),
        event("task_started", turn_id="grand-turn"),
        *trigger("/root/planner/reviewer"),
        event("task_complete", turn_id="grand-turn", last_agent_message="grand done"),
    ])

    model = CodexModel(str(root))
    model.poll()

    child_node = model.nodes["cx:" + CHILD_ID]
    grand_node = model.nodes["cx:" + GRAND_ID]
    child_spawn = next(step for step in child_node.steps if step.kind == "spawn")
    assert grand_node.parent == child_node.id
    assert grand_node.status == "done"
    assert child_spawn.child == grand_node.id
    assert child_spawn.status == "done"


def test_codex_spawn_sequences_form_stable_dispatches(tmp_path):
    path = rollout_path(tmp_path, ROOT_ID)
    child_ids = [
        f"019f0000-0000-7000-8000-{index:012d}"
        for index in range(10, 19)
    ]
    records = [root_meta()]

    def add_pair(start, boundary=None):
        for offset in range(2):
            index = start + offset
            call_id = f"spawn-{index}"
            records.extend([
                spawn_call(call_id, f"agent-{index}"),
                activity(child_ids[index], f"/root/agent-{index}", call_id),
                response("function_call_output", call_id=call_id, output="started"),
            ])
        if boundary:
            records.append(boundary)

    add_pair(
        0,
        response("reasoning", summary=[{"type": "summary_text", "text": "check results"}]),
    )
    add_pair(
        2,
        response("message", role="assistant",
                 content=[{"type": "output_text", "text": "next work"}]),
    )
    add_pair(
        4,
        response("function_call", name="wait_agent", call_id="wait-1",
                 arguments=json.dumps({"ids": ["agent"]})),
    )
    add_pair(
        6,
        response("function_call", name="list_agents", call_id="list-1", arguments="{}"),
    )
    records.extend([
        spawn_call("spawn-8", "agent-8"),
        activity(child_ids[8], "/root/agent-8", "spawn-8"),
    ])
    write_jsonl(path, records)

    model = CodexModel(str(path))
    assert model.poll() is True

    batches = [node for node in model.nodes.values() if node.kind == "dispatch"]
    assert [node.id for node in batches] == [
        "dispatch:main:spawn-0",
        "dispatch:main:spawn-2",
        "dispatch:main:spawn-4",
        "dispatch:main:spawn-6",
    ]
    assert [
        [model.nodes[child_id].label for child_id in batch.children]
        for batch in batches
    ] == [
        ["agent-0", "agent-1"],
        ["agent-2", "agent-3"],
        ["agent-4", "agent-5"],
        ["agent-6", "agent-7"],
    ]
    last = model.nodes["cx:" + child_ids[8]]
    assert last.parent == "main"
    assert last.id in model.nodes["main"].children


def test_codex_item_completed_activity_attaches_dispatch_children(tmp_path):
    ids = [
        "019f0000-0000-7000-8000-000000000070",
        "019f0000-0000-7000-8000-000000000071",
    ]
    root = rollout_path(tmp_path, ROOT_ID)
    write_jsonl(root, [
        root_meta(),
        spawn_call("first", "first"),
        completed_activity(ids[0], "/root/first", "first"),
        spawn_call("second", "second"),
        completed_activity(ids[1], "/root/second", "second"),
    ])
    for index, thread_id in enumerate(ids):
        write_jsonl(rollout_path(tmp_path, thread_id, f"10-0{index + 1}-00"), [
            child_meta(
                thread_id,
                ROOT_ID,
                f"/root/{'first' if index == 0 else 'second'}",
            ),
        ])

    model = CodexModel(str(root))
    settle_model(model)

    batch = model.nodes["dispatch:main:first"]
    assert model.nodes["main"].children == [batch.id]
    assert batch.children == ["cx:" + thread_id for thread_id in ids]


def test_normalized_codex_nodes_render_as_recursive_tree(tmp_path):
    root = rollout_path(tmp_path, ROOT_ID)
    child = rollout_path(tmp_path, CHILD_ID, "10-01-00")
    write_jsonl(root, [root_meta(), spawn_call(), activity()])
    write_jsonl(child, [child_meta(), *trigger("/root/planner")])

    model = CodexModel(str(root))
    model.poll()
    text, _, order, view = render_tree(model, "main")

    assert order == ["main", "cx:" + CHILD_ID]
    assert view["cx:" + CHILD_ID].parent == "main"
    assert "planner" in text.plain


def test_codex_dispatch_tree_summarizes_multiple_agents_in_order(tmp_path):
    ids = [
        f"019f0000-0000-7000-8000-{index:012d}"
        for index in range(30, 36)
    ]
    root = rollout_path(tmp_path, ROOT_ID)
    write_jsonl(root, [
        root_meta(),
        spawn_call("single", "single"),
        activity(ids[0], "/root/single", "single"),
        response("reasoning", summary=[{"type": "summary_text", "text": "next dispatch"}]),
        spawn_call("group-a", "alpha"),
        activity(ids[1], "/root/alpha", "group-a"),
        spawn_call("group-b", "beta"),
        activity(ids[2], "/root/beta", "group-b"),
        spawn_call("group-c", "gamma"),
        activity(ids[3], "/root/gamma", "group-c"),
    ])
    write_jsonl(rollout_path(tmp_path, ids[1], "10-01-00"), [
        child_meta(ids[1], ROOT_ID, "/root/alpha", "alpha"),
        event("task_started", turn_id="alpha-turn"),
        *trigger("/root/alpha"),
        event("task_complete", turn_id="alpha-turn", last_agent_message="done"),
    ])
    write_jsonl(rollout_path(tmp_path, ids[2], "10-02-00"), [
        child_meta(ids[2], ROOT_ID, "/root/beta", "beta"),
        event("task_started", turn_id="beta-turn"),
        *trigger("/root/beta"),
        spawn_call("nested-a", "delta"),
        activity(ids[4], "/root/beta/delta", "nested-a"),
        spawn_call("nested-b", "epsilon"),
        activity(ids[5], "/root/beta/epsilon", "nested-b"),
    ])
    write_jsonl(rollout_path(tmp_path, ids[3], "10-03-00"), [
        child_meta(ids[3], ROOT_ID, "/root/gamma", "gamma"),
        event("task_started", turn_id="gamma-turn"),
        *trigger("/root/gamma"),
        event("turn_aborted", turn_id="gamma-turn", reason="failed"),
    ])

    model = CodexModel(str(root))
    model.poll()
    text, _, order, view = render_tree(model, "main")

    top_batch = "dispatch:main:group-a"
    nested_batch = f"dispatch:cx:{ids[2]}:nested-a"
    assert order == [
        "main",
        "cx:" + ids[0],
        top_batch,
        "cx:" + ids[1],
        "cx:" + ids[2],
        nested_batch,
        "cx:" + ids[4],
        "cx:" + ids[5],
        "cx:" + ids[3],
    ]
    assert view["cx:" + ids[0]].parent == "main"
    assert view[top_batch].parent == "main"
    assert view[nested_batch].parent == "cx:" + ids[2]
    assert model.nodes[top_batch].label == ""
    assert "3 agents · 1 running · 1 done · 1 error" in text.plain


def test_codex_dispatch_children_follow_spawn_order_when_activity_arrives_reversed(tmp_path):
    ids = [
        f"019f0000-0000-7000-8000-{index:012d}"
        for index in range(60, 66)
    ]
    root = rollout_path(tmp_path, ROOT_ID)
    write_jsonl(root, [
        root_meta(),
        spawn_call("top-a", "top-a"),
        spawn_call("top-b", "top-b"),
        activity(ids[1], "/root/top-b", "top-b"),
        activity(ids[0], "/root/top-a", "top-a"),
    ])
    write_jsonl(rollout_path(tmp_path, ids[1], "10-01-00"), [
        child_meta(ids[1], ROOT_ID, "/root/top-b", "top-b"),
        event("task_started", turn_id="top-b-turn"),
        *trigger("/root/top-b"),
        spawn_call("nested-a", "nested-a"),
        spawn_call("nested-b", "nested-b"),
        activity(ids[3], "/root/top-b/nested-b", "nested-b"),
        activity(ids[2], "/root/top-b/nested-a", "nested-a"),
    ])

    model = CodexModel(str(root))
    model.poll()

    top_batch = "dispatch:main:top-a"
    nested_batch = f"dispatch:cx:{ids[1]}:nested-a"
    assert model.nodes[top_batch].children == ["cx:" + ids[0], "cx:" + ids[1]]
    assert model.nodes[nested_batch].children == ["cx:" + ids[2], "cx:" + ids[3]]

    append_jsonl(root, [
        spawn_call("top-c", "top-c"),
        spawn_call("top-d", "top-d"),
        activity(ids[5], "/root/top-d", "top-d"),
        activity(ids[4], "/root/top-c", "top-c"),
    ])
    model.poll()

    assert model.nodes[top_batch].children == [
        "cx:" + ids[0],
        "cx:" + ids[1],
        "cx:" + ids[4],
        "cx:" + ids[5],
    ]


def test_codex_batch_label_uses_task_name_and_keeps_its_batch_id_live(tmp_path):
    ids = [
        f"019f0000-0000-7000-8000-{index:012d}"
        for index in range(40, 43)
    ]
    root = rollout_path(tmp_path, ROOT_ID)
    write_jsonl(root, [
        root_meta(),
        spawn_call("first", "Review parser docs"),
        activity(ids[0], "/root/first", "first"),
        spawn_call("second", "Review parser tests"),
        activity(ids[1], "/root/second", "second"),
    ])
    model = CodexModel(str(root))
    model.poll()
    batch_id = "dispatch:main:first"
    assert model.nodes[batch_id].label == "Review parser"

    append_jsonl(root, [
        spawn_call("third", "Review parser edge cases"),
        activity(ids[2], "/root/third", "third"),
    ])
    model.poll()

    assert model.nodes["main"].children == [batch_id]
    assert model.nodes[batch_id].children == ["cx:" + child_id for child_id in ids]
    assert model.nodes[batch_id].label == "Review parser"


def test_active_quiet_complete_reopen_and_abort(tmp_path):
    path = rollout_path(tmp_path, ROOT_ID)
    write_jsonl(path, [root_meta(), event("task_started", turn_id="turn-1")])
    model = CodexModel(str(path))
    model.poll()
    assert model.nodes["main"].status == "running"
    assert model.poll() is False
    assert model.nodes["main"].status == "running"

    append_jsonl(path, [event("task_complete", turn_id="turn-1", last_agent_message="done")])
    assert model.poll() is True
    assert model.nodes["main"].status == "done"

    append_jsonl(path, [
        event("task_started", turn_id="turn-2"),
        event("turn_aborted", turn_id="turn-2", reason="interrupted"),
    ])
    assert model.poll() is True
    assert model.nodes["main"].status == "error"
    assert model.nodes["main"].result == "interrupted"


def test_output_before_call_and_explicit_call_failure(tmp_path):
    path = rollout_path(tmp_path, ROOT_ID)
    write_jsonl(path, [
        root_meta(),
        response("function_call_output", call_id="late-call", output="ready"),
        response("function_call", name="list_agents", namespace="collaboration",
                 call_id="late-call", arguments="{}"),
        response("custom_tool_call", name="exec", call_id="failed-call",
                 input="bad command", status="failed"),
    ])
    model = CodexModel(str(path))
    model.poll()
    first, second = model.nodes["main"].steps
    assert first.status == "done"
    assert first.output == "ready"
    assert second.status == "error"


def test_partial_malformed_and_blank_lines_are_exactly_once(tmp_path):
    path = rollout_path(tmp_path, ROOT_ID)
    write_jsonl(path, [root_meta()])
    with path.open("a") as fh:
        fh.write("\n{not json}\n")
    model = CodexModel(str(path))
    assert model.poll() is False
    assert model.nodes["main"].steps == []
    write_jsonl(path, [root_meta()])
    assert model.poll() is True

    message = event("agent_message", message="arrived once", phase="commentary")
    encoded = json.dumps(message)
    split = len(encoded) // 2
    with path.open("a") as fh:
        fh.write(encoded[:split])
    assert model.poll() is False
    assert model.nodes["main"].steps == []

    with path.open("a") as fh:
        fh.write(encoded[split:] + "\n")
    assert model.poll() is True
    assert [step.body for step in model.nodes["main"].steps] == ["arrived once"]
    assert model.poll() is False


def test_concurrent_append_after_stat_is_not_duplicated(tmp_path, monkeypatch):
    path = rollout_path(tmp_path, ROOT_ID)
    write_jsonl(path, [root_meta()])
    model = CodexModel(str(path))
    model.poll()
    append_jsonl(path, [event("agent_message", message="first", phase="commentary")])
    second = event("agent_message", message="second", phase="commentary")
    original_open = builtins.open
    target = os.path.realpath(path)
    appended = False

    class AppendAfterSuffixRead:
        def __init__(self, wrapped):
            self.wrapped = wrapped

        def __enter__(self):
            self.wrapped.__enter__()
            return self

        def __exit__(self, *args):
            return self.wrapped.__exit__(*args)

        def __getattr__(self, name):
            return getattr(self.wrapped, name)

        def readline(self, *args, **kwargs):
            nonlocal appended
            data = self.wrapped.readline(*args, **kwargs)
            if data and not appended:
                appended = True
                append_jsonl(path, [second])
            return data

    def append_during_read(candidate, *args, **kwargs):
        wrapped = original_open(candidate, *args, **kwargs)
        if os.path.realpath(os.path.abspath(os.fspath(candidate))) == target:
            return AppendAfterSuffixRead(wrapped)
        return wrapped

    monkeypatch.setattr(provider_reload, "open", append_during_read, raising=False)
    assert model.poll() is True
    assert [step.body for step in model.nodes["main"].steps] == ["first"]
    assert model.poll() is True
    assert [step.body for step in model.nodes["main"].steps] == ["first", "second"]
    assert model.poll() is False


def test_new_child_file_is_discovered_incrementally(tmp_path):
    root = rollout_path(tmp_path, ROOT_ID)
    child = rollout_path(tmp_path, CHILD_ID, "10-01-00")
    write_jsonl(root, [root_meta(), spawn_call(), activity()])
    model = CodexModel(str(root))
    model.poll()
    placeholder = model.nodes["cx:" + CHILD_ID]
    assert placeholder.status == "running"

    time.sleep(0.002)
    write_jsonl(child, [
        child_meta(),
        event("task_started", turn_id="child-turn"),
        *trigger("/root/planner"),
        event("task_complete", turn_id="child-turn", last_agent_message="discovered"),
    ])
    assert model.poll() is True
    assert model.nodes["cx:" + CHILD_ID].status == "done"
    assert model.nodes["cx:" + CHILD_ID].detail == "Curie"


def test_old_root_bootstrap_discovers_child_from_current_day(tmp_path):
    root = rollout_path(tmp_path, ROOT_ID)
    sessions_dir = tmp_path / "sessions"
    current_dir = sorted(codex_model._date_dirs(str(sessions_dir)))[0]
    child = os.path.join(current_dir, "rollout-current-" + CHILD_ID + ".jsonl")
    write_jsonl(root, [root_meta()])
    write_jsonl(Path(child), [child_meta()])

    model = CodexModel(str(root))

    assert model.poll() is True
    assert model.nodes["cx:" + CHILD_ID].label == "agent"
    assert os.path.dirname(str(root)) in model.observation_paths()
    assert current_dir in model.observation_paths()


def test_child_bootstrap_resolves_root_from_its_uuid_date(tmp_path):
    sessions = tmp_path / "sessions"
    root = (
        sessions / "2026" / "06" / "25" /
        f"rollout-2026-06-25T11-17-07-{ROOT_ID}.jsonl"
    )
    child = rollout_path(tmp_path, CHILD_ID, "10-01-00")
    write_jsonl(root, [
        root_meta(),
        spawn_call(task_name="review_parser"),
        activity(path="/root/review_parser"),
    ])
    write_jsonl(child, [child_meta(path="/root/review_parser")])

    model = CodexModel(str(child))
    settle_model(model)

    assert model.path == os.path.realpath(root)
    assert model.nodes["main"].id == "main"
    assert model.nodes["cx:" + CHILD_ID].parent == "main"
    assert model.nodes["cx:" + CHILD_ID].label == "review_parser"


def test_codex_reload_does_not_scan_unrelated_historic_directories(
        tmp_path, monkeypatch):
    root = rollout_path(tmp_path, ROOT_ID)
    historic = tmp_path / "sessions" / "2001" / "02" / "03"
    historic.mkdir(parents=True)
    write_jsonl(root, [root_meta()])
    real_scandir = os.scandir
    scanned = []

    def guarded_scandir(path):
        candidate = os.path.realpath(os.path.abspath(os.fspath(path)))
        if candidate == os.path.realpath(historic):
            raise AssertionError("scanned unrelated historic Codex directory")
        scanned.append(candidate)
        return real_scandir(path)

    monkeypatch.setattr(provider_reload.os, "scandir", guarded_scandir)
    model = CodexModel(str(root))

    assert model.poll() is True
    assert os.path.realpath(tmp_path / "sessions") not in scanned
    assert os.path.realpath(root.parent) in scanned


@pytest.mark.parametrize("prior_entry_kind", ["missing", "directory"])
def test_codex_relevant_directory_name_and_kind_changes_invalidate_candidates(
        tmp_path, prior_entry_kind):
    root = rollout_path(tmp_path, ROOT_ID)
    child = rollout_path(tmp_path, CHILD_ID, "10-01-00")
    write_jsonl(root, rewrite_records("root work"))
    if prior_entry_kind == "directory":
        child.mkdir(parents=True)
    session = CodexModel(str(root))
    settle_model(session)
    directory = child.parent
    previous = directory.stat()

    if prior_entry_kind == "directory":
        child.rmdir()
    write_jsonl(child, rewrite_child_records("child work"))
    os.utime(directory, ns=(previous.st_atime_ns, previous.st_mtime_ns))
    assert directory.stat().st_mtime_ns == previous.st_mtime_ns

    assert session.poll() is True
    assert [step.body for step in session.nodes["cx:" + CHILD_ID].steps] == [
        "child work"
    ]
    assert session.poll() is False


def test_codex_rewritten_irrelevant_path_is_reconsidered_from_current_identity(
        tmp_path):
    root = rollout_path(tmp_path, ROOT_ID)
    candidate = rollout_path(tmp_path, CHILD_ID, "10-01-00")
    write_jsonl(root, rewrite_records("root work"))
    write_jsonl(candidate, [child_meta(session_id=REPLACEMENT_ID)])
    session = CodexModel(str(root))
    settle_model(session)

    assert session.accepts_observation(frozenset({str(candidate)})) is False

    replacement = candidate.with_suffix(".replacement")
    write_jsonl(replacement, rewrite_child_records("child work"))
    os.replace(replacement, candidate)
    assert session.accepts_observation(frozenset({str(candidate)})) is True
    assert session.poll() is True
    assert [step.body for step in session.nodes["cx:" + CHILD_ID].steps] == [
        "child work"
    ]

@pytest.mark.parametrize("candidate_state", ["pending", "irrelevant"])
def test_codex_unaccepted_file_to_directory_can_later_admit(
        tmp_path, candidate_state):
    root = rollout_path(tmp_path, ROOT_ID)
    candidate = rollout_path(tmp_path, CHILD_ID, "10-01-00")
    write_jsonl(root, rewrite_records("root work"))
    if candidate_state == "pending":
        candidate.parent.mkdir(parents=True, exist_ok=True)
        candidate.write_text("")
    else:
        write_jsonl(candidate, [child_meta(session_id=REPLACEMENT_ID)])
    session = CodexModel(str(root))
    settle_model(session)

    candidate.unlink()
    candidate.mkdir()
    assert session.poll() is False
    candidate.rmdir()
    write_jsonl(candidate, rewrite_child_records("recovered child"))

    assert session.poll() is True
    assert [step.body for step in session.nodes["cx:" + CHILD_ID].steps] == [
        "recovered child",
    ]


def test_codex_accepted_file_to_directory_stays_frozen(tmp_path):
    root = rollout_path(tmp_path, ROOT_ID)
    child = rollout_path(tmp_path, CHILD_ID, "10-01-00")
    write_jsonl(root, rewrite_records("root work"))
    write_jsonl(child, rewrite_child_records("old child"))
    session = CodexModel(str(root))
    settle_model(session)
    expected = public_projection(session)

    child.unlink()
    child.mkdir()
    assert session.poll() is False
    child.rmdir()
    write_jsonl(child, rewrite_child_records("replacement child"))

    assert session.poll() is False
    assert public_projection(session) == expected


def test_empty_child_candidate_is_retried_when_content_arrives(tmp_path):
    root = rollout_path(tmp_path, ROOT_ID)
    child = rollout_path(tmp_path, CHILD_ID, "10-01-00")
    write_jsonl(root, [root_meta(), spawn_call(), activity()])
    child.parent.mkdir(parents=True, exist_ok=True)
    child.write_text("")
    model = CodexModel(str(root))
    model.poll()
    assert str(child) in model._pending_candidates

    append_jsonl(child, [
        child_meta(),
        event("task_started", turn_id="child-turn"),
        *trigger("/root/planner"),
        event("task_complete", turn_id="child-turn", last_agent_message="discovered"),
    ])
    assert model.poll() is True
    assert model.nodes["cx:" + CHILD_ID].status == "done"
    assert str(child) not in model._pending_candidates


def test_internal_guardian_rollout_is_excluded(tmp_path):
    root = rollout_path(tmp_path, ROOT_ID)
    guardian = rollout_path(tmp_path, GUARDIAN_ID, "10-01-00")
    write_jsonl(root, [
        root_meta(),
        event("sub_agent_activity", agent_thread_id=GUARDIAN_ID,
              event_id="call-guardian", kind="started"),
    ])
    write_jsonl(guardian, [guardian_meta(), event("task_started", turn_id="guard-turn")])

    model = CodexModel(str(root))
    model.poll()
    assert "cx:" + GUARDIAN_ID not in model.nodes
    assert all(state.thread_id != GUARDIAN_ID for state in model._states.values())


def test_child_path_resolves_root_and_missing_root_falls_back(tmp_path):
    root = rollout_path(tmp_path, ROOT_ID)
    child = rollout_path(tmp_path, CHILD_ID, "10-01-00")
    write_jsonl(root, [root_meta()])
    write_jsonl(child, [
        child_meta(),
        event("task_started", turn_id="child-turn"),
        *trigger("/root/planner"),
    ])

    resolved = CodexModel(str(child))
    resolved.poll()
    assert resolved.path == str(root)
    assert "cx:" + CHILD_ID in resolved.nodes

    orphan_id = "019f0000-0000-7000-8000-000000000099"
    orphan_path = rollout_path(tmp_path / "orphan", CHILD_ID)
    write_jsonl(orphan_path, [
        child_meta(session_id=orphan_id),
        root_meta(orphan_id),
        event("task_started", turn_id="copied-turn"),
        record("turn_context", {"turn_id": "copied-turn", "model": "copied-model"}),
        event("agent_message", message="copied work", phase="commentary"),
        event("token_count", info={"total_token_usage": {"output_tokens": 999}}),
        event("task_complete", turn_id="copied-turn", last_agent_message="copied result"),
        event("task_started", turn_id="orphan-turn"),
        record("turn_context", {"turn_id": "orphan-turn", "model": "orphan-model"}),
        *trigger("/root/planner"),
        event("agent_message", message="orphan work", phase="commentary"),
        event("token_count", info={"total_token_usage": {"output_tokens": 7}}),
    ])
    fallback = CodexModel(str(orphan_path))
    fallback.poll()
    assert fallback.path == str(orphan_path)
    assert [step.body for step in fallback.nodes["main"].steps] == ["orphan work"]
    assert fallback.nodes["main"].status == "running"
    assert fallback.nodes["main"].result == ""
    assert fallback.nodes["main"].model == "orphan-model"
    assert fallback.nodes["main"].tokens == 7


@pytest.mark.parametrize(
    "first_record",
    [
        pytest.param([], id="array"),
        pytest.param(7, id="number"),
        pytest.param("text", id="string"),
        pytest.param(None, id="null"),
    ],
)
def test_non_object_first_record_denies_codex_rollout(tmp_path, first_record):
    path = rollout_path(tmp_path, ROOT_ID)
    write_jsonl(path, [
        first_record,
        root_meta(),
        event("agent_message", message="must stay hidden", phase="commentary"),
    ])
    model = CodexModel(str(path))

    assert model.poll() is False
    assert model.nodes["main"].steps == []
    assert model.poll() is False


def test_invalid_rollout_is_rejected(tmp_path):
    path = tmp_path / "bad.jsonl"
    path.write_text(json.dumps({"type": "assistant"}) + "\n")
    model = CodexModel(str(path))
    assert model.poll() is False
    assert model.nodes["main"].steps == []


def test_observation_rejects_unrelated_rollout_writes(tmp_path):
    root = rollout_path(tmp_path, ROOT_ID)
    child = rollout_path(tmp_path, CHILD_ID, "10-01-00")
    unrelated = rollout_path(tmp_path, GRAND_ID, "10-02-00")
    write_jsonl(root, [root_meta()])
    write_jsonl(child, [child_meta()])
    write_jsonl(unrelated, [root_meta(GRAND_ID)])
    model = CodexModel(str(root))
    settle_model(model)

    assert model.accepts_observation({str(child)})
    assert not model.accepts_observation({str(unrelated)})
    assert not model.accepts_observation({str(unrelated)})


@pytest.mark.parametrize(
    "tail",
    [pytest.param(b"{not json}\n", id="malformed"), pytest.param(b'{"type":', id="incomplete")],
)
def test_known_foreign_rollout_body_does_not_block_target_load_or_append(
        tmp_path, tail):
    root = rollout_path(tmp_path, ROOT_ID)
    foreign = rollout_path(tmp_path, REPLACEMENT_ID, "10-01-00")
    write_jsonl(root, rewrite_records("target initial"))
    foreign.parent.mkdir(parents=True, exist_ok=True)
    foreign.write_bytes((json.dumps(root_meta(REPLACEMENT_ID)) + "\n").encode() + tail)
    session = CodexModel(str(root))

    assert session.poll() is True
    assert [step.body for step in session.nodes["main"].steps] == ["target initial"]
    assert session.poll() is False

    append_jsonl(root, [
        event("agent_message", message="target append", phase="commentary"),
    ])
    assert session.poll() is True
    assert [step.body for step in session.nodes["main"].steps] == [
        "target initial", "target append",
    ]
    assert session.poll() is False


@pytest.mark.parametrize(
    "first_record",
    [
        pytest.param(b"{not json}\n", id="malformed"),
        pytest.param(b'{"type":', id="incomplete"),
        pytest.param(
            b'{"type":"session_meta","payload":\n',
            id="malformed-session-meta",
        ),
        pytest.param(
            b'{"type":"session_meta","payload":',
            id="incomplete-session-meta",
        ),
        pytest.param(
            b'{"type":"session_meta","payload":{"id":"bad\xff"}}\n',
            id="invalid-utf8-session-meta",
        ),
        pytest.param(
            b'{"type":"session_meta","payload":{"id":"foreign",'
            b'"future":NaN}}\n',
            id="non-finite-session-meta",
        ),
        pytest.param(
            b'{"type":"session_meta","payload":'
            + b"[" * 10_000 + b"0" + b"]" * 10_000 + b"}\n",
            id="deep-session-meta",
        ),
    ],
)
def test_foreign_invalid_first_record_is_denied_without_stalling_target(
        tmp_path, first_record):
    root = rollout_path(tmp_path, ROOT_ID)
    foreign = rollout_path(tmp_path, REPLACEMENT_ID, "10-01-00")
    write_jsonl(root, rewrite_records("target initial"))
    foreign.parent.mkdir(parents=True, exist_ok=True)
    foreign.write_bytes(first_record)
    session = CodexModel(str(root))

    assert session.poll() is True
    assert [step.body for step in session.nodes["main"].steps] == ["target initial"]
    assert session.poll() is False
    assert session.accepts_observation(frozenset({str(foreign)})) is False
    assert not any(
        claim.kind == "file" and claim.path == os.path.realpath(foreign)
        for claim in CodexSnapshotCodec().capture(session).sources
    )

    append_jsonl(root, [
        event("agent_message", message="target append", phase="commentary"),
    ])
    assert session.poll() is True
    assert [step.body for step in session.nodes["main"].steps] == [
        "target initial", "target append",
    ]
    assert session.poll() is False

    replacement = foreign.with_suffix(".replacement")
    write_jsonl(replacement, [
        child_meta(
            REPLACEMENT_ID,
            parent=ROOT_ID,
            session_id=ROOT_ID,
        ),
        event("agent_message", message="recovered child", phase="commentary"),
    ])
    os.replace(replacement, foreign)
    assert session.accepts_observation(frozenset({str(foreign)})) is True
    assert session.poll() is True
    assert [
        step.body for step in session.nodes["cx:" + REPLACEMENT_ID].steps
    ] == ["recovered child"]
    assert session.poll() is False


def test_codex_admission_hashes_escaped_unknown_root_bytes_without_stall(
        tmp_path):
    root = rollout_path(tmp_path, ROOT_ID)
    foreign = rollout_path(tmp_path, REPLACEMENT_ID, "10-01-00")
    root.parent.mkdir(parents=True, exist_ok=True)
    metadata = json.dumps(root_meta(), separators=(",", ":")).encode("utf-8")
    metadata = metadata[:-1] + b',"future":"\\ud800"}\n'
    body = json.dumps(
        event("agent_message", message="target", phase="commentary"),
        separators=(",", ":"),
    ).encode("utf-8") + b"\n"
    root.write_bytes(metadata + body)
    write_jsonl(foreign, [root_meta(REPLACEMENT_ID)])
    session = CodexModel(str(root))

    assert session.poll() is True
    assert [step.body for step in session.nodes["main"].steps] == ["target"]
    assert session.poll() is False

    append_jsonl(root, [
        event("agent_message", message="target append", phase="commentary"),
    ])
    assert session.poll() is True
    assert [step.body for step in session.nodes["main"].steps] == [
        "target", "target append",
    ]
    assert session.poll() is False


def test_changed_admitted_child_full_stamp_freezes_child(
        tmp_path):
    root = rollout_path(tmp_path, ROOT_ID)
    child = rollout_path(tmp_path, CHILD_ID, "10-01-00")
    linked = tmp_path / "requested.jsonl"
    write_jsonl(root, rewrite_records("target initial"))
    write_jsonl(child, rewrite_child_records("child initial"))
    linked.symlink_to(root)
    session = CodexModel(str(linked))
    settle_model(session)
    assert session.path == os.path.realpath(root)
    assert "cx:" + CHILD_ID in session.nodes

    before = child.stat()
    original = child.read_bytes()
    malformed_prefix = b'{"type":"session_meta","payload":'
    assert len(original) > len(malformed_prefix) + 1
    time.sleep(0.01)
    child.write_bytes(
        malformed_prefix
        + b"x" * (len(original) - len(malformed_prefix) - 1)
        + b"\n"
    )
    os.utime(child, ns=(before.st_atime_ns, before.st_mtime_ns))
    after = child.stat()
    assert (
        before.st_dev,
        before.st_ino,
        before.st_mode,
        before.st_size,
        before.st_mtime_ns,
    ) == (
        after.st_dev,
        after.st_ino,
        after.st_mode,
        after.st_size,
        after.st_mtime_ns,
    )
    assert after.st_ctime_ns != before.st_ctime_ns

    append_jsonl(root, [
        event("agent_message", message="target append", phase="commentary"),
    ])
    assert session.poll() is True
    settle_model(session)
    assert [step.body for step in session.nodes["main"].steps] == [
        "target initial", "target append",
    ]
    assert [step.body for step in session.nodes["cx:" + CHILD_ID].steps] == [
        "child initial",
    ]


def test_target_append_does_not_claim_unchanged_foreign_rollout(tmp_path):
    root = rollout_path(tmp_path, ROOT_ID)
    foreign = rollout_path(tmp_path, REPLACEMENT_ID, "10-01-00")
    write_jsonl(root, rewrite_records("target initial"))
    write_jsonl(foreign, [
        root_meta(REPLACEMENT_ID),
        event("agent_message", message="foreign", phase="commentary"),
    ])
    session = CodexModel(str(root))
    settle_model(session)

    append_jsonl(root, [
        event("agent_message", message="target append", phase="commentary"),
    ])
    assert session.poll() is True
    snapshot = CodexSnapshotCodec().capture(session)
    foreign_path = os.path.realpath(foreign)
    assert not any(
        claim.kind == "file" and claim.path == foreign_path
        for claim in snapshot.sources
    )
    assert [step.body for step in session.nodes["main"].steps] == [
        "target initial", "target append",
    ]
    assert session.poll() is False


def test_codex_root_identity_change_freezes_current_group(
        tmp_path):
    root = rollout_path(tmp_path, ROOT_ID)
    child = rollout_path(tmp_path, CHILD_ID, "10-01-00")
    write_jsonl(root, rewrite_records("group A", thread_id=ROOT_ID))
    write_jsonl(child, [
        child_meta(
            CHILD_ID,
            parent=REPLACEMENT_ID,
            session_id=REPLACEMENT_ID,
        ),
        event("agent_message", message="group B child", phase="commentary"),
    ])
    session = CodexModel(str(root))
    settle_model(session)
    assert session.accepts_observation(frozenset({str(child)})) is False
    assert "cx:" + CHILD_ID not in session.nodes

    write_jsonl(root, rewrite_records("group B", thread_id=REPLACEMENT_ID))
    assert session.poll() is False
    assert session.lifecycle_identity() == ("codex", ROOT_ID)
    assert "cx:" + CHILD_ID not in session.nodes


@pytest.mark.parametrize("replacement", [False, True], ids=["appended", "replaced"])
def test_known_foreign_denial_freezes_append_and_reconsiders_replacement(
        tmp_path, monkeypatch, replacement):
    root = rollout_path(tmp_path, ROOT_ID)
    foreign = rollout_path(tmp_path, REPLACEMENT_ID, "10-01-00")
    write_jsonl(root, rewrite_records("target"))
    write_jsonl(foreign, [root_meta(REPLACEMENT_ID)])
    session = CodexModel(str(root))
    settle_model(session)
    assert session.accepts_observation(frozenset({str(foreign)})) is False

    records = [
        child_meta(REPLACEMENT_ID, session_id=ROOT_ID),
        event("agent_message", message="new child", phase="commentary"),
    ]
    if replacement:
        temporary = foreign.with_suffix(".replacement")
        write_jsonl(temporary, records)
        os.replace(temporary, foreign)
    else:
        write_jsonl(foreign, records)

    def forbidden_open(*_args, **_kwargs):
        raise AssertionError("observation classification opened transcript")

    with monkeypatch.context() as scope:
        scope.setattr(session_identity, "open", forbidden_open, raising=False)
        assert session.accepts_observation(frozenset({str(foreign)})) is replacement

    assert session.poll() is replacement
    if replacement:
        assert [
            step.body
            for step in session.nodes["cx:" + REPLACEMENT_ID].steps
        ] == ["new child"]
    else:
        assert "cx:" + REPLACEMENT_ID not in session.nodes


def test_repeated_known_foreign_observation_is_pure_and_opens_no_transcript(
        tmp_path, monkeypatch):
    root = rollout_path(tmp_path, ROOT_ID)
    foreign = rollout_path(tmp_path, REPLACEMENT_ID, "10-01-00")
    write_jsonl(root, rewrite_records("target"))
    write_jsonl(foreign, [root_meta(REPLACEMENT_ID)])
    session = CodexModel(str(root))
    settle_model(session)
    codec = CodexSnapshotCodec()
    before = codec.capture(session)

    def forbidden_open(*_args, **_kwargs):
        raise AssertionError("observation classification opened transcript")

    with monkeypatch.context() as scope:
        scope.setattr(session_identity, "open", forbidden_open, raising=False)
        scope.setattr(provider_reload, "open", forbidden_open, raising=False)
        assert session.accepts_observation(frozenset({str(foreign)})) is False
        assert session.accepts_observation(frozenset({str(foreign)})) is False

    after = codec.capture(session)
    assert after.sources == before.sources
    assert after.payload == before.payload


def test_codex_accepts_observation_is_pure_for_source_admission_state(tmp_path):
    root = rollout_path(tmp_path, ROOT_ID)
    write_jsonl(root, rewrite_records("root"))
    model = CodexModel(str(root))
    settle_model(model)
    unrelated = tmp_path / "outside" / "unrelated.jsonl"
    write_jsonl(unrelated, [root_meta(REPLACEMENT_ID)])
    codec = CodexSnapshotCodec()
    before = copy.deepcopy(codec.capture(model).payload)

    assert model.accepts_observation(frozenset({str(unrelated)})) is False

    assert codec.capture(model).payload == before


def test_codex_workflow_and_skill_signals(tmp_path):
    ticket = ".scratch/stack/issues/03-blackboard.md"
    path = rollout_path(tmp_path, ROOT_ID)
    write_jsonl(path, [
        root_meta(),
        response("custom_tool_call", name="exec", call_id="w1",
                 input='await tools.exec_command({cmd:"sed -n 1,60p '
                       '/s/skills/to-tickets/SKILL.md"});'),
        response("custom_tool_call", name="exec", call_id="w2", input=(
            'const patch = "*** Begin Patch\\n*** Add File: %s\\n'
            '+**Status:** ready-for-agent\\n*** End Patch";\n'
            'text(await tools.apply_patch(patch));' % ticket)),
        response("custom_tool_call", name="exec", call_id="w3", input=(
            'const patch = "*** Begin Patch\\n*** Update File: %s\\n@@\\n'
            '-**Status:** ready-for-agent\\n+**Status:** in-progress\\n*** End Patch";\n'
            'text(await tools.apply_patch(patch));' % ticket)),
        response("custom_tool_call", name="exec", call_id="w4",
                 input='await tools.exec_command({cmd:"blackboard show --project ."});'),
        # Codex encrypts a spawn message, so the dispatch carries no review verdict.
        response("function_call", name="spawn_agent", call_id="w6",
                 arguments=json.dumps({"task_name": "reviewer",
                                       "message": "gAAAAABqaqV49BPS_StGKEcrXQyzuvw12345"})),
    ])
    model = CodexModel(str(path))
    model.poll()
    main = model.nodes["main"]
    assert main.skills == ["to-tickets"]
    assert main.workflow == ["ticket 03-blackboard created",
                            "ticket 03-blackboard → in-progress",
                            "board show", "agent dispatched"]
    assert main.phase == "tickets"      # the loaded skill, not the ticket or board events


def test_codex_signals_from_bare_tool_calls(tmp_path):
    """The shapes Codex writes without its exec wrapper: a `function_call exec_command`
    carrying `cmd`, and an `apply_patch` whose input is the patch text itself."""
    ticket = ".scratch/stack/issues/03-blackboard.md"
    path = rollout_path(tmp_path, ROOT_ID)
    write_jsonl(path, [
        root_meta(),
        response("function_call", name="exec_command", call_id="r1",
                 arguments=json.dumps({"cmd": "sed -n '1,240p' /s/skills/to-tickets/SKILL.md",
                                       "workdir": "/w", "yield_time_ms": 10000})),
        response("custom_tool_call", name="apply_patch", call_id="r2",
                 input="*** Begin Patch\n*** Add File: %s\n"
                       "+**Status:** ready-for-agent\n*** End Patch" % ticket),
        response("function_call", name="exec_command", call_id="r4",
                 arguments=json.dumps({"cmd": "blackboard show --project .", "workdir": "/w"})),
    ])
    model = CodexModel(str(path))
    model.poll()
    main = model.nodes["main"]
    assert main.skills == ["to-tickets"]
    assert main.workflow == ["ticket 03-blackboard created", "board show"]
    assert main.phase == "tickets"


@pytest.mark.parametrize(
    "first_record",
    [
        pytest.param(
            {"type": "session_meta", "payload": {"id": 7}},
            id="invalid-codex-identity",
        ),
        pytest.param(
            {"type": "session", "version": 3, "id": "pi", "cwd": "/work"},
            id="foreign-provider",
        ),
        pytest.param(
            {"type": "event_msg", "payload": {}},
            id="non-session",
        ),
    ],
)
def test_codex_model_never_uses_later_metadata_after_first_record_denial(
    tmp_path, first_record,
):
    path = rollout_path(tmp_path, ROOT_ID)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        _raw_record(first_record)
        + _raw_record(root_meta())
        + _raw_record(event(
            "agent_message", message="must stay hidden", phase="commentary",
        ))
    )
    model = CodexModel(str(path))

    assert model.poll() is False
    assert model.nodes["main"].steps == []
    assert model.poll() is False


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param("read", id="read-oserror"),
        pytest.param("stat", id="stat-oserror"),
        pytest.param("disappear", id="disappears-after-listing"),
    ],
)
def test_unadmitted_candidate_io_failure_cannot_block_target_load_or_append(
    tmp_path, monkeypatch, failure,
):
    root = rollout_path(tmp_path, ROOT_ID)
    foreign = rollout_path(tmp_path, REPLACEMENT_ID, "10-01-00")
    write_jsonl(root, rewrite_records("target initial"))
    write_jsonl(foreign, [root_meta(REPLACEMENT_ID)])
    target = os.path.realpath(foreign)

    if failure == "read":
        original_open = builtins.open

        def fail_candidate_read(path, *args, **kwargs):
            if os.path.realpath(os.fspath(path)) == target:
                raise PermissionError("candidate cannot be read")
            return original_open(path, *args, **kwargs)

        monkeypatch.setattr(
            provider_reload, "open", fail_candidate_read, raising=False,
        )
    else:
        original_stamp = provider_reload._regular_file_stamp
        failed = False

        def fail_candidate_stat(path):
            nonlocal failed
            if os.path.realpath(os.fspath(path)) == target:
                if failure == "disappear" and not failed:
                    foreign.unlink()
                    failed = True
                    raise FileNotFoundError("candidate disappeared")
                raise PermissionError("candidate cannot be stated")
            return original_stamp(path)

        monkeypatch.setattr(provider_reload, "_regular_file_stamp", fail_candidate_stat)

    model = CodexModel(str(root))
    assert model.poll() is True
    assert [step.body for step in model.nodes["main"].steps] == ["target initial"]
    assert model.poll() is False

    append_jsonl(root, [
        event("agent_message", message="target append", phase="commentary"),
    ])
    assert model.poll() is True
    assert [step.body for step in model.nodes["main"].steps] == [
        "target initial", "target append",
    ]
    assert model.poll() is False


@pytest.mark.parametrize(
    ("depth", "accepted"),
    [
        pytest.param(128, True, id="depth-128"),
        pytest.param(129, False, id="depth-129"),
    ],
)
def test_codex_runtime_uses_shared_nesting_boundary(
    tmp_path, depth, accepted,
):
    root = rollout_path(tmp_path, ROOT_ID)
    write_jsonl(root, rewrite_records("old value"))
    model = CodexModel(str(root))
    settle_model(model)
    expected = public_projection(model)
    addition = event(
        "agent_message", message="boundary value", phase="commentary",
    )
    addition["future"] = _nested_value(depth - 1)
    append_jsonl(root, [addition])

    assert model.poll() is accepted
    if accepted:
        assert [step.body for step in model.nodes["main"].steps] == [
            "old value", "boundary value",
        ]
    else:
        assert public_projection(model) == expected
    assert model.poll() is False


@pytest.mark.parametrize(
    ("digits", "accepted"),
    [
        pytest.param(128, True, id="128-digits"),
        pytest.param(129, False, id="129-digits"),
    ],
)
def test_codex_known_output_tokens_uses_exact_digit_boundary(
    tmp_path, digits, accepted,
):
    root = rollout_path(tmp_path, ROOT_ID)
    write_jsonl(root, [
        root_meta(),
        event("agent_message", message="old value", phase="commentary"),
        event("token_count", info={
            "total_token_usage": {"output_tokens": 7},
        }),
    ])
    model = CodexModel(str(root))
    settle_model(model)
    expected = public_projection(model)
    addition = _raw_record(event("token_count", info={
        "total_token_usage": {"output_tokens": "LONG_INTEGER"},
    })).replace(b'"LONG_INTEGER"', b"9" * digits)
    with root.open("ab") as stream:
        stream.write(addition)

    assert model.poll() is accepted
    if accepted:
        assert model.nodes["main"].tokens == int("9" * digits)
    else:
        assert public_projection(model) == expected
        assert model.nodes["main"].tokens == 7
    assert model.poll() is False


@pytest.mark.parametrize(
    "token",
    [
        pytest.param(b"1" + b"0" * 128, id="129-digit-integer"),
        pytest.param(b"1e999", id="large-finite-exponent"),
    ],
)
def test_codex_unknown_numeric_token_does_not_reject_visible_record(
    tmp_path, token,
):
    root = rollout_path(tmp_path, ROOT_ID)
    write_jsonl(root, rewrite_records("old value"))
    model = CodexModel(str(root))
    settle_model(model)
    addition = _raw_record(event(
        "agent_message", message="accepted value", phase="commentary",
    ))
    addition = addition[:-2] + b',"future":' + token + b"}\n"
    with root.open("ab") as stream:
        stream.write(addition)

    assert model.poll() is True
    assert [step.body for step in model.nodes["main"].steps] == [
        "old value", "accepted value",
    ]
    assert model.poll() is False
