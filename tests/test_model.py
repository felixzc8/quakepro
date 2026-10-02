"""Golden-tree tests for model.py — the parser that tracks Claude Code's private,
evolving on-disk transcript format. If that format drifts, these break loudly
instead of the pane silently rendering a wrong tree."""
import builtins
import copy
import json
import os
import threading
import time

import pytest

import quakepro.claude_model as model
import quakepro.provider_reload as provider_reload
from quakepro.claude_snapshot import ClaudeSnapshotCodec
from quakepro.lifecycle import SessionLifecycle
from quakepro.claude_model import ClaudeModel, _text_of
from quakepro.session_model import LifecycleFact, parse_timestamp


JSONL_RECORD_LIMIT = 64 * 1024 * 1024


def write_jsonl(path, records):
    with open(path, "w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def append_jsonl(path, records):
    with open(path, "a") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def main_turn(tool_id, sub="code-reviewer", desc="review code", ts="2026-06-29T10:00:00"):
    return {
        "type": "assistant",
        "timestamp": ts,
        "message": {
            "timestamp": ts,
            "usage": {"output_tokens": 10},
            "content": [
                {"type": "text", "text": "spawning an agent"},
                {"type": "tool_use", "id": tool_id, "name": "Task",
                 "input": {"subagent_type": sub, "description": desc}},
            ],
        },
    }


def main_dispatch_turn(*calls, ts="2026-06-29T10:00:00"):
    return {
        "type": "assistant",
        "timestamp": ts,
        "message": {
            "timestamp": ts,
            "content": [
                {"type": "tool_use", "id": tool_id, "name": name,
                 "input": {"subagent_type": sub, "description": desc}}
                for tool_id, name, sub, desc in calls
            ],
        },
    }


def progress(agent_id, tool_id, prompt="do the thing", model_name="claude-sonnet-4-6",
             tokens=42, ts="2026-06-29T10:00:05", uuid=None, content=None):
    wrapped = {
        "timestamp": ts,
        "message": {
            "role": "assistant",
            "model": model_name,
            "usage": {"output_tokens": tokens},
            "content": content or [{"type": "text", "text": "working on it"}],
        },
    }
    if uuid:
        wrapped["uuid"] = uuid
    return {
        "type": "progress",
        "toolUseID": tool_id,
        "data": {
            "type": "agent_progress",
            "agentId": agent_id,
            "prompt": prompt,
            "message": wrapped,
        },
    }


def tool_result(tool_id, is_error=False, ts="2026-06-29T10:00:10"):
    c = {"type": "tool_result", "tool_use_id": tool_id, "content": "finished"}
    if is_error:
        c["is_error"] = True
    return {"type": "user", "timestamp": ts, "message": {"content": [c]}}


def root_text(text, padding=""):
    return {
        "type": "assistant",
        "padding": padding,
        "message": {
            "timestamp": "2026-06-29T10:00:00",
            "content": [{"type": "text", "text": text}],
        },
    }


def claude_semantic_replacement(case):
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
            name = "Bash"
        elif field == "patch":
            name = "apply_patch"
        elif field in {
            "prompt", "message", "task", "description", "instructions",
            "task_name",
        }:
            name = "Task"
        else:
            name = "Edit"
        return [root_text("partial new"), {
            "type": "assistant",
            "message": {"content": [{
                "type": "tool_use",
                "id": "recognizer-call",
                "name": name,
                "input": {field: value},
            }]},
        }]
    if case == "assistant-timestamp":
        invalid = root_text("typed field")
        invalid["message"]["timestamp"] = 7
        return [root_text("partial new"), invalid]
    if case in {"thinking-text", "text-block-text"}:
        field = "thinking" if case == "thinking-text" else "text"
        return [root_text("partial new"), {
            "type": "assistant",
            "message": {"content": [{"type": field.split("-")[0], field: 7}]},
        }]
    if case.startswith("tool-use-"):
        block = {
            "type": "tool_use",
            "id": "tool-1",
            "name": "Task",
            "input": {
                "subagent_type": "researcher",
                "description": "inspect source",
                "prompt": "inspect source",
            },
        }
        if case == "tool-use-id":
            block["id"] = 7
        elif case == "tool-use-name":
            block["name"] = 7
        elif case == "tool-use-input":
            block["input"] = ["not", "an", "object"]
        elif case == "tool-use-description":
            block["input"]["description"] = 7
        elif case == "tool-use-prompt":
            block["input"]["prompt"] = 7
        return [root_text("partial new"), {
            "type": "assistant", "message": {"content": [block]},
        }]
    if case.startswith("tool-result-"):
        result = {
            "type": "tool_result",
            "tool_use_id": "tool-1",
            "content": "done",
            "is_error": False,
        }
        if case == "tool-result-id":
            result["tool_use_id"] = 7
        else:
            result["is_error"] = "false"
        return [root_text("partial new"), {
            "type": "user", "message": {"content": [result]},
        }]
    invalid = progress("child", "tool-1", content=[{
        "type": "text", "text": "child work",
    }])
    if case == "progress-agent-id":
        invalid["data"]["agentId"] = 7
    elif case == "progress-tool-use-id":
        invalid["toolUseID"] = 7
    elif case == "progress-prompt":
        invalid["data"]["prompt"] = 7
    elif case == "progress-uuid":
        invalid["data"]["message"]["uuid"] = 7
    elif case == "progress-model":
        invalid["data"]["message"]["message"]["model"] = 7
    elif case == "progress-output-tokens":
        invalid["data"]["message"]["message"]["usage"]["output_tokens"] = "42"
    return [root_text("partial new"), invalid]


def flat_child_record(record_id, text):
    return {
        "uuid": record_id,
        "timestamp": "2026-06-29T10:00:01",
        "message": {
            "role": "assistant",
            "usage": {"output_tokens": 1},
            "content": [{"type": "text", "text": text}],
        },
    }


def public_projection(session):
    return copy.deepcopy((
        session.lifecycle_identity(),
        session.nodes,
        session.task_list,
    ))


def rewrite_during_tail_read(monkeypatch, path, offset, replacement):
    race_started = threading.Event()
    rewrite_done = threading.Event()

    def rewrite_source():
        if not race_started.wait(timeout=5):
            rewrite_done.set()
            return
        path.write_bytes(replacement)
        rewrite_done.set()

    writer = threading.Thread(target=rewrite_source, daemon=True)
    writer.start()
    original_open = builtins.open
    target_path = os.path.realpath(path)
    raced = False

    class RacingReader:
        def __init__(self, wrapped):
            self.wrapped = wrapped
            self.position = 0

        def __enter__(self):
            self.wrapped.__enter__()
            return self

        def __exit__(self, *args):
            return self.wrapped.__exit__(*args)

        def __getattr__(self, name):
            return getattr(self.wrapped, name)

        def read(self, *args, **kwargs):
            nonlocal raced
            data = self.wrapped.read(*args, **kwargs)
            self.position += len(data)
            if self.position >= offset and not raced:
                raced = True
                race_started.set()
                assert rewrite_done.wait(timeout=5)
            return data

    def open_after_rewrite(candidate, *args, **kwargs):
        mode = args[0] if args else kwargs.get("mode", "r")
        target = os.path.realpath(os.path.abspath(os.fspath(candidate))) == target_path
        wrapped = original_open(candidate, *args, **kwargs)
        return RacingReader(wrapped) if target and "b" in mode else wrapped

    monkeypatch.setattr(provider_reload, "open", open_after_rewrite, raising=False)
    return writer


@pytest.mark.parametrize(
    ("old_padding", "new_padding"),
    [("x" * 256, ""), ("x" * 32, "y" * 32), ("", "y" * 256)],
)
def test_claude_accepted_rewrite_freezes_projection(
        tmp_path, old_padding, new_padding):
    path = tmp_path / "session.jsonl"
    write_jsonl(path, [root_text("old value", old_padding)])
    session = ClaudeModel(str(path))
    while session.poll():
        pass
    expected = public_projection(session)

    write_jsonl(path, [root_text("new value", new_padding)])

    assert session.poll() is False
    assert public_projection(session) == expected


@pytest.mark.parametrize(
    "case",
    [
        "assistant-timestamp",
        "thinking-text",
        "text-block-text",
        "tool-use-id",
        "tool-use-name",
        "tool-use-input",
        "tool-use-description",
        "tool-use-prompt",
        "tool-result-id",
        "tool-result-is-error",
        "progress-agent-id",
        "progress-tool-use-id",
        "progress-prompt",
        "progress-uuid",
        "progress-model",
        "progress-output-tokens",
        "recognizer-input-skill",
        "recognizer-input-notebook_path",
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
        "recognizer-input-message",
        "recognizer-input-task",
        "recognizer-input-instructions",
        "recognizer-input-task_name",
        "recognizer-input-edits",
        "recognizer-input-edits-item",
    ],
)
def test_claude_appended_records_require_exact_graph_types(tmp_path, case):
    path = tmp_path / "session.jsonl"
    write_jsonl(path, [root_text("old value")])
    session = ClaudeModel(str(path))
    while session.poll():
        pass
    expected = public_projection(session)

    append_jsonl(path, claude_semantic_replacement(case))

    assert session.poll() is False
    assert public_projection(session) == expected
    assert session.poll() is False
    assert public_projection(session) == expected


@pytest.mark.parametrize("record_type", ["assistant", "user"])
@pytest.mark.parametrize(
    "invalid",
    [pytest.param("false", id="string"), pytest.param(0, id="integer")],
)
def test_claude_appended_sidechain_requires_exact_bool(
        tmp_path, invalid, record_type):
    path = tmp_path / "session.jsonl"
    write_jsonl(path, [root_text("old value")])
    session = ClaudeModel(str(path))
    while session.poll():
        pass
    expected = public_projection(session)
    record = {
        "type": record_type,
        "isSidechain": invalid,
        "message": {
            "role": record_type,
            "content": [{"type": "text", "text": "partial"}],
        },
    }

    append_jsonl(path, [record])

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
def test_claude_appended_non_finite_json_is_atomic(tmp_path, non_finite):
    path = tmp_path / "session.jsonl"
    write_jsonl(path, [root_text("old value")])
    session = ClaudeModel(str(path))
    while session.poll():
        pass
    expected = public_projection(session)
    invalid = [root_text("partial new")]
    invalid[-1]["future"] = {"scores": [non_finite]}

    append_jsonl(path, invalid)

    assert session.poll() is False
    assert public_projection(session) == expected


def test_claude_root_user_text_becomes_prompt_without_tool_results_becoming_prompts(tmp_path):
    p = tmp_path / "session.jsonl"
    user_turn = {
        "type": "user",
        "timestamp": "2026-06-29T09:59:59",
        "message": {"content": [{"type": "text", "text": "Improve session display"}]},
    }
    write_jsonl(p, [
        user_turn,
        main_turn("tool_1"),
        progress("agent_a", "tool_1"),
        tool_result("tool_1"),
    ])

    m = ClaudeModel(str(p))
    assert m.poll() is True

    prompts = [step.body for step in m.nodes["main"].steps if step.kind == "prompt"]
    assert prompts == ["Improve session display"]
    assert m.nodes["agent_a"].result == "finished"

def test_spawn_then_progress_then_done(tmp_path):
    p = tmp_path / "session.jsonl"
    write_jsonl(p, [main_turn("tool_1"), progress("agent_a", "tool_1"), tool_result("tool_1")])
    m = ClaudeModel(str(p))
    assert m.poll() is True

    assert "agent_a" in m.nodes
    node = m.nodes["agent_a"]
    assert node.parent == "main"
    assert node.label == "code-reviewer"          # named by subagent_type, not model
    assert node.status == "done"
    assert node.model == "sonnet"                 # claude-sonnet-4-6 -> sonnet
    assert node.tokens == 42
    assert "agent_a" in m.nodes["main"].children


def test_claude_child_label_uses_agent_type_only(tmp_path):
    p = tmp_path / "session.jsonl"
    write_jsonl(p, [
        main_turn("named", sub="code-reviewer", desc="Review named work"),
        progress("named-agent", "named"),
        main_turn("unnamed", sub="", desc="Description is not a name"),
        progress("unnamed-agent", "unnamed"),
    ])

    m = ClaudeModel(str(p))
    assert m.poll() is True

    assert m.nodes["named-agent"].label == "code-reviewer"
    assert m.nodes["unnamed-agent"].label == "agent"


def test_claude_task_and_agent_calls_in_one_message_form_stable_dispatches(tmp_path):
    p = tmp_path / "session.jsonl"
    write_jsonl(p, [
        main_dispatch_turn(
            ("first-task", "Task", "researcher", "Review parser docs"),
            ("first-agent", "Agent", "reviewer", "Review parser tests"),
        ),
        main_dispatch_turn(
            ("second-task", "Task", "writer", "write draft"),
            ("second-agent", "Agent", "editor", "edit draft"),
            ts="2026-06-29T10:01:00",
        ),
        progress("research", "first-task"),
        progress("review", "first-agent"),
        progress("write", "second-task"),
        progress("edit", "second-agent"),
    ])

    m = ClaudeModel(str(p))
    assert m.poll() is True

    assert m.nodes["main"].children == [
        "dispatch:main:first-task", "dispatch:main:second-task",
    ]
    assert m.nodes["dispatch:main:first-task"].children == ["research", "review"]
    assert m.nodes["dispatch:main:second-task"].children == ["write", "edit"]
    assert m.nodes["dispatch:main:first-task"].label == "Review parser"
    assert m.nodes["dispatch:main:second-task"].label == ""
    assert m.nodes["research"].parent == "dispatch:main:first-task"
    assert m.nodes["edit"].parent == "dispatch:main:second-task"


def test_claude_dispatch_does_not_invent_title_without_shared_goal(tmp_path):
    p = tmp_path / "session.jsonl"
    write_jsonl(p, [main_dispatch_turn(
        ("first", "Task", "researcher", ""),
        ("second", "Agent", "reviewer", ""),
    ), main_dispatch_turn(
        ("third", "Task", "general-purpose", ""),
        ("fourth", "Agent", "agent", ""),
    )])

    model = ClaudeModel(str(p))
    model.poll()

    assert model.nodes["dispatch:main:first"].label == ""
    assert model.nodes["dispatch:main:third"].label == ""


def test_late_claude_subagent_files_join_their_dispatch_after_lifecycle(tmp_path):
    p = tmp_path / "session.jsonl"
    write_jsonl(p, [main_dispatch_turn(
        ("task-call", "Task", "researcher", "find facts"),
        ("agent-call", "Agent", "reviewer", "review facts"),
    )])
    m = ClaudeModel(str(p))
    m.poll()

    lifecycle = SessionLifecycle(m)
    facts = [
        LifecycleFact(
            "claude", "session", "SubagentStop", agent_id=agent_id
        )
        for agent_id in ("late-task", "late-agent")
    ]
    assert lifecycle.ingest(facts) is False
    subagents = tmp_path / "session" / "subagents"
    os.makedirs(subagents)
    for agent_id, tool_id in (("late-task", "task-call"),
                              ("late-agent", "agent-call")):
        write_jsonl(subagents / f"agent-{agent_id}.jsonl", [])
        with open(subagents / f"agent-{agent_id}.meta.json", "w") as f:
            json.dump({"agentType": "general-purpose", "toolUseId": tool_id}, f)

    assert m.poll() is True
    assert lifecycle.enforce() is True

    batch = m.nodes["dispatch:main:task-call"]
    assert batch.children == ["late-task", "late-agent"]
    assert batch.status == "done"
    assert m.poll() is False


def test_unmatched_empty_claude_stop_waits_for_agent_evidence(tmp_path):
    p = tmp_path / "session.jsonl"
    write_jsonl(p, [])
    model = ClaudeModel(str(p))
    lifecycle = SessionLifecycle(model)
    fact = LifecycleFact(
        "claude",
        "session",
        "SubagentStop",
        agent_id="late-agent",
        result="Loading WebSearch and WebFetch tools",
    )

    assert lifecycle.ingest([fact]) is False
    assert "late-agent" not in model.nodes

    append_jsonl(p, [progress("late-agent", "late-call")])

    assert model.poll() is True
    assert lifecycle.enforce() is True
    assert model.nodes["late-agent"].status == "done"
    assert model.nodes["late-agent"].result == (
        "Loading WebSearch and WebFetch tools"
    )


def test_claude_lifecycle_enforcement_settles_after_long_result(tmp_path):
    p = tmp_path / "session.jsonl"
    write_jsonl(p, [])
    model = ClaudeModel(str(p))
    lifecycle = SessionLifecycle(model)
    fact = LifecycleFact(
        "claude", "session", "SessionEnd", result="x" * 201
    )

    assert lifecycle.ingest([fact]) is True
    assert model.nodes["main"].result == "x" * 200
    assert lifecycle.enforce() is False


def test_nested_claude_dispatch_stays_beneath_its_spawning_agent(tmp_path):
    p = tmp_path / "session.jsonl"
    nested = progress(
        "parent", "outer", uuid="parent-message", content=[
            {"type": "tool_use", "id": "nested-task", "name": "Task",
             "input": {"subagent_type": "researcher", "description": "find facts"}},
            {"type": "tool_use", "id": "nested-agent", "name": "Agent",
             "input": {"subagent_type": "reviewer", "description": "review facts"}},
        ],
    )
    write_jsonl(p, [
        main_turn("outer"), nested,
        progress("research", "nested-task"),
        progress("review", "nested-agent"),
    ])

    m = ClaudeModel(str(p))
    m.poll()

    assert m.nodes["parent"].parent == "main"
    assert m.nodes["parent"].children == ["dispatch:parent:nested-task"]
    assert m.nodes["dispatch:parent:nested-task"].children == ["research", "review"]
    assert m.nodes["research"].parent == "dispatch:parent:nested-task"


def test_single_claude_task_or_agent_remains_a_direct_child(tmp_path):
    p = tmp_path / "session.jsonl"
    write_jsonl(p, [main_dispatch_turn(
        ("only-task", "Task", "researcher", "find facts"),
    ), progress("research", "only-task")])

    m = ClaudeModel(str(p))
    m.poll()

    assert m.nodes["main"].children == ["research"]
    assert m.nodes["research"].parent == "main"
    assert not any(node.kind == "dispatch" for node in m.nodes.values())


def test_task_agent_result_captured_on_node(tmp_path):
    # The spawn's tool_result IS the agent's final message; keep a short form on
    # the node, like workflow agents carry from their journal.
    p = tmp_path / "session.jsonl"
    fin = {"type": "tool_result", "tool_use_id": "tool_1", "content": "shipped the fix"}
    write_jsonl(p, [main_turn("tool_1"), progress("agent_a", "tool_1"),
                    {"type": "user", "message": {"content": [fin]}}])
    m = ClaudeModel(str(p))
    m.poll()
    assert m.nodes["agent_a"].result == "shipped the fix"


def test_agent_result_survives_result_before_progress(tmp_path):
    # Result text can land before the agent's substream — _pending_result carries it.
    p = tmp_path / "session.jsonl"
    fin = {"type": "tool_result", "tool_use_id": "tool_1", "content": "done early"}
    write_jsonl(p, [main_turn("tool_1"),
                    {"type": "user", "message": {"content": [fin]}},
                    progress("agent_a", "tool_1")])
    m = ClaudeModel(str(p))
    m.poll()
    assert m.nodes["agent_a"].status == "done"
    assert m.nodes["agent_a"].result == "done early"


def test_error_result_marks_node_error(tmp_path):
    p = tmp_path / "session.jsonl"
    write_jsonl(p, [main_turn("tool_1"), progress("agent_a", "tool_1"),
                    tool_result("tool_1", is_error=True)])
    m = ClaudeModel(str(p))
    m.poll()
    assert m.nodes["agent_a"].status == "error"


def test_root_api_error_marks_failed_session_and_preserves_message(tmp_path):
    p = tmp_path / "session.jsonl"
    failure = root_text("Failed to authenticate: OAuth session expired")
    failure.update(isApiErrorMessage=True, error="authentication_failed")
    write_jsonl(p, [failure])
    m = ClaudeModel(str(p))
    m.poll()

    assert m.nodes["main"].status == "error"
    assert m.nodes["main"].result == failure["message"]["content"][0]["text"]
    assert m.nodes["main"].steps[0].body == m.nodes["main"].result
    assert not m.poll()
    assert m.nodes["main"].status == "error"


@pytest.mark.parametrize("retry", [
    {"type": "user", "message": {"content": "Try again"}},
    root_text("The request succeeded"),
])
def test_root_api_error_clears_when_session_retries(tmp_path, retry):
    p = tmp_path / "session.jsonl"
    failure = root_text("Request failed")
    failure["isApiErrorMessage"] = True
    write_jsonl(p, [failure])
    m = ClaudeModel(str(p))
    m.poll()
    assert m.nodes["main"].status == "error"

    append_jsonl(p, [retry])
    m.poll()

    assert m.nodes["main"].status == "running"
    assert m.nodes["main"].result == ""


def test_root_api_error_survives_historical_and_terminal_hooks(tmp_path):
    p = tmp_path / "session.jsonl"
    failure = root_text("Failed to authenticate")
    failure["isApiErrorMessage"] = True
    write_jsonl(p, [failure])
    m = ClaudeModel(str(p))
    m.poll()
    failed_ns = int(m.nodes["main"].last_ts * 1_000_000_000)
    lifecycle = SessionLifecycle(m)

    lifecycle.ingest([
        LifecycleFact("claude", "session", "SessionStart", recorded_ns=failed_ns - 2),
        LifecycleFact("claude", "session", "UserPromptSubmit", recorded_ns=failed_ns - 1),
        LifecycleFact("claude", "session", "Stop", recorded_ns=failed_ns + 1),
        LifecycleFact("claude", "session", "SessionEnd", recorded_ns=failed_ns + 2),
    ])
    lifecycle.enforce()

    assert lifecycle.ended
    assert m.nodes["main"].status == "error"
    assert m.nodes["main"].result == "Failed to authenticate"

    lifecycle.ingest([
        LifecycleFact("claude", "session", "UserPromptSubmit", recorded_ns=failed_ns + 3),
    ])
    assert not lifecycle.ended
    assert m.nodes["main"].status == "running"
    assert m.nodes["main"].result == ""


def test_backfilled_substream_holds_agent_running_until_burst_drains(tmp_path):
    # Claude Code writes a spawn's tool_result to the transcript BEFORE it backfills
    # the agent's buffered progress substream. The result must not flip the agent
    # "done" while that substream is still streaming in — it stays "running" until a
    # poll goes by with no fresh progress, then settles.
    p = tmp_path / "session.jsonl"
    write_jsonl(p, [main_turn("tool_1"), tool_result("tool_1"),
                    progress("agent_a", "tool_1", ts="2026-06-29T10:00:01"),
                    progress("agent_a", "tool_1", ts="2026-06-29T10:00:02")])
    m = ClaudeModel(str(p))
    m.poll()
    assert m.nodes["agent_a"].status == "running"   # backfill still arriving

    append_jsonl(p, [progress("agent_a", "tool_1", ts="2026-06-29T10:00:03")])
    m.poll()
    assert m.nodes["agent_a"].status == "running"   # more backfill this poll

    m.poll()                                        # end-of-burst pass -> settle
    assert m.nodes["agent_a"].status == "done"


def test_incremental_tail_picks_up_appended_lines(tmp_path):
    p = tmp_path / "session.jsonl"
    write_jsonl(p, [main_turn("tool_1")])
    m = ClaudeModel(str(p))
    m.poll()
    assert m.nodes["main"].children == ["agent_a"] or "agent_a" not in m.nodes
    off = m._offset
    assert off > 0

    append_jsonl(p, [progress("agent_a", "tool_1"), tool_result("tool_1")])
    assert m.poll() is True
    assert m._offset > off
    assert m.nodes["agent_a"].status == "done"

    # A poll with nothing new appended is a no-op.
    assert m.poll() is False


def test_main_tail_waits_for_complete_jsonl_record(tmp_path):
    p = tmp_path / "session.jsonl"
    encoded = json.dumps(main_turn("tool_1"))
    split = len(encoded) // 2
    p.write_text(encoded[:split])
    m = ClaudeModel(str(p))

    assert m.poll() is False
    assert m._offset == 0
    with open(p, "a") as f:
        f.write(encoded[split:] + "\n")

    assert m.poll() is True
    assert "tool_1" in m._spawn_owner


def test_live_and_flat_agent_records_are_deduplicated(tmp_path):
    tool = {"type": "tool_use", "id": "inner_tool", "name": "Read",
            "input": {"file_path": "/tmp/example.py"}}
    live_tool = progress("agent_a", "spawn_tool", tokens=7, uuid="message-1", content=[tool])
    result_content = [{"type": "tool_result", "tool_use_id": "inner_tool", "content": "contents"}]
    live_result = {
        "type": "progress", "toolUseID": "spawn_tool",
        "data": {"type": "agent_progress", "agentId": "agent_a", "prompt": "inspect",
                 "message": {"uuid": "message-2", "timestamp": "2026-06-29T10:00:06",
                             "message": {"role": "user", "content": result_content}}},
    }
    p = tmp_path / "session.jsonl"
    write_jsonl(p, [main_turn("spawn_tool"), live_tool, live_result])
    sub = tmp_path / "session" / "subagents"
    os.makedirs(sub)
    write_jsonl(sub / "agent-agent_a.jsonl", [
        {"uuid": "message-1", "timestamp": "2026-06-29T10:00:05",
         "message": live_tool["data"]["message"]["message"]},
        {"uuid": "message-2", "timestamp": "2026-06-29T10:00:06",
         "message": {"role": "user", "content": result_content}},
    ])
    with open(sub / "agent-agent_a.meta.json", "w") as f:
        json.dump({"agentType": "code-reviewer", "toolUseId": "spawn_tool"}, f)

    m = ClaudeModel(str(p))
    m.poll()
    node = m.nodes["agent_a"]
    tool_steps = [step for step in node.steps if step.tid == "inner_tool"]
    assert node.tokens == 7
    assert len(tool_steps) == 1
    assert tool_steps[0].status == "done"
    assert tool_steps[0].output == "contents"


def test_teammate_disk_then_live_reuses_node_and_deduplicates(tmp_path, monkeypatch):
    teams = tmp_path / "teams"
    monkeypatch.setattr(model, "TEAMS", str(teams))
    p = tmp_path / "session.jsonl"
    write_jsonl(p, [])
    sub = tmp_path / "session" / "subagents"
    os.makedirs(sub)
    disk_record = {"uuid": "same-message", "timestamp": "2026-06-29T10:00:05",
                   "message": {"role": "assistant", "model": "claude-sonnet-4-6",
                               "usage": {"output_tokens": 5},
                               "content": [{"type": "text", "text": "working on it"}]}}
    write_jsonl(sub / "agent-a1.jsonl", [disk_record])
    with open(sub / "agent-a1.meta.json", "w") as f:
        json.dump(_team_member_meta(name="alice"), f)
    cfg_dir = teams / "session-xyz"
    os.makedirs(cfg_dir)
    with open(cfg_dir / "config.json", "w") as f:
        json.dump({"members": [{"name": "alice", "agentType": "general-purpose"}]}, f)
    m = ClaudeModel(str(p))
    m.poll()
    assert m.nodes["tm:session-xyz:a1"].tokens == 5

    append_jsonl(p, [main_turn("spawn-1"),
                     progress("a1", "spawn-1", tokens=5, uuid="same-message")])
    m.poll()
    nid = "tm:session-xyz:a1"
    assert "a1" not in m.nodes
    assert m.nodes[nid].tokens == 5
    assert len([step for step in m.nodes[nid].steps if step.kind == "text"]) == 1
    assert m.nodes[nid].parent == "team:session-xyz"
    assert m._spawn["spawn-1"] == nid
    assert m._step_by_tid["spawn-1"].child == nid


def test_teammate_live_then_disk_reclassifies_without_duplication(tmp_path, monkeypatch):
    teams = tmp_path / "teams"
    monkeypatch.setattr(model, "TEAMS", str(teams))
    p = tmp_path / "session.jsonl"
    write_jsonl(p, [main_turn("spawn-1"),
                    progress("a1", "spawn-1", tokens=5, uuid="same-message")])
    m = ClaudeModel(str(p))
    m.poll()
    assert m.nodes["a1"].tokens == 5

    sub = tmp_path / "session" / "subagents"
    os.makedirs(sub)
    write_jsonl(sub / "agent-a1.jsonl", [
        {"uuid": "same-message", "timestamp": "2026-06-29T10:00:05",
         "message": progress("a1", "spawn-1", tokens=5)["data"]["message"]["message"]},
    ])
    with open(sub / "agent-a1.meta.json", "w") as f:
        json.dump(_team_member_meta(name="alice"), f)
    cfg_dir = teams / "session-xyz"
    os.makedirs(cfg_dir)
    with open(cfg_dir / "config.json", "w") as f:
        json.dump({"members": [{"name": "alice", "agentType": "general-purpose"}]}, f)
    m.poll()
    nid = "tm:session-xyz:a1"
    assert "a1" not in m.nodes
    assert m.nodes[nid].tokens == 5
    assert m._spawn["spawn-1"] == nid
    assert m._step_by_tid["spawn-1"].child == nid


def test_agent_tool_result_and_use_pair_in_either_order(tmp_path):
    tool_content = [{"type": "tool_use", "id": "inner-tool", "name": "Read",
                     "input": {"file_path": "/tmp/a.py"}}]
    result_content = [{"type": "tool_result", "tool_use_id": "inner-tool",
                       "content": "file contents"}]
    for name, roles in (("result-first", ("user", "assistant")),
                        ("use-first", ("assistant", "user"))):
        records = []
        for index, role in enumerate(roles):
            content = result_content if role == "user" else tool_content
            records.append({
                "type": "progress", "toolUseID": "spawn",
                "data": {"type": "agent_progress", "agentId": "a1",
                         "message": {"uuid": "%s-%s" % (name, role),
                                     "timestamp": "2026-06-29T10:00:0%s" % index,
                                     "message": {"role": role, "content": content}}},
            })
        p = tmp_path / (name + ".jsonl")
        write_jsonl(p, records)
        m = ClaudeModel(str(p))
        m.poll()
        steps = [step for step in m.nodes["a1"].steps if step.tid == "inner-tool"]
        assert len(steps) == 1
        assert steps[0].status == "done"
        assert steps[0].output == "file contents"


def test_nested_spawn_result_before_use_replays_terminal_state(tmp_path):
    result_content = [{"type": "tool_result", "tool_use_id": "nested-spawn",
                       "content": "nested child finished"}]
    result_first = {
        "type": "progress",
        "data": {"type": "agent_progress", "agentId": "parent",
                 "message": {"uuid": "result-first", "timestamp": "2026-06-29T10:00:01",
                             "message": {"role": "user", "content": result_content}}},
    }
    spawn_use = progress(
        "parent", "outer", uuid="spawn-use", tokens=1,
        content=[{"type": "tool_use", "id": "nested-spawn", "name": "Task",
                  "input": {"subagent_type": "general-purpose", "prompt": "nested work"}}],
        ts="2026-06-29T10:00:02",
    )
    child = progress("child", "nested-spawn", uuid="child-message", tokens=2,
                     ts="2026-06-29T10:00:03")
    p = tmp_path / "session.jsonl"
    write_jsonl(p, [result_first, spawn_use, child])
    m = ClaudeModel(str(p))
    m.poll()

    assert m._step_by_tid["nested-spawn"].status == "done"
    assert m.nodes["child"].status == "done"
    assert m.nodes["child"].result == "nested child finished"
    assert m.nodes["child"].parent == "parent"


def test_result_then_child_then_spawn_use_resolves_deferred_edge(tmp_path):
    result_content = [{"type": "tool_result", "tool_use_id": "nested-spawn",
                       "content": "finished before declaration"}]
    result_first = {
        "type": "progress",
        "data": {"type": "agent_progress", "agentId": "parent",
                 "message": {"uuid": "early-result", "timestamp": "2026-06-29T10:00:01",
                             "message": {"role": "user", "content": result_content}}},
    }
    child = progress("child", "agent-message-id", uuid="early-child", tokens=2,
                     ts="2026-06-29T10:00:02")
    child["parentToolUseID"] = "nested-spawn"
    spawn_use = progress(
        "parent", "outer", uuid="late-spawn-use", tokens=1,
        content=[{"type": "tool_use", "id": "nested-spawn", "name": "Agent",
                  "input": {"subagent_type": "code-reviewer", "prompt": "nested work"}}],
        ts="2026-06-29T10:00:03",
    )
    p = tmp_path / "session.jsonl"
    write_jsonl(p, [result_first, child, spawn_use])
    m = ClaudeModel(str(p))
    m.poll()

    assert m._spawn["nested-spawn"] == "child"
    assert m._step_by_tid["nested-spawn"].child == "child"
    assert m.nodes["child"].parent == "parent"
    assert m.nodes["child"].label == "code-reviewer"
    assert m.nodes["child"].status == "done"
    assert m.nodes["child"].result == "finished before declaration"
    assert "nested-spawn" not in m._pending_spawn_children
    assert "agent-message-id" not in m._pending_spawn_children
    assert m.nodes["parent"].children.count("child") == 1

    append_jsonl(p, [child])
    m.poll()
    assert m.nodes["parent"].children.count("child") == 1
    assert m.nodes["child"].tokens == 2


def test_fallback_dedup_preserves_repeated_same_source_occurrences(tmp_path):
    repeated = progress("a1", "spawn", tokens=3, ts="2026-06-29T10:00:05.123456")
    p = tmp_path / "session.jsonl"
    write_jsonl(p, [repeated, repeated])
    sub = tmp_path / "session" / "subagents"
    os.makedirs(sub)
    disk = {"timestamp": "2026-06-29T10:00:05.123456",
            "message": repeated["data"]["message"]["message"]}
    write_jsonl(sub / "agent-a1.jsonl", [disk, disk])
    m = ClaudeModel(str(p))
    m.poll()

    assert m.nodes["a1"].tokens == 6
    assert len([step for step in m.nodes["a1"].steps if step.kind == "text"]) == 2
    first = m._agent_record_key({}, disk["message"], "2026-06-29T10:00:05.123456")
    second = m._agent_record_key({}, disk["message"], "2026-06-29T10:00:05.123457")
    assert first != second


def test_stable_uuid_is_absolute_across_sources_and_occurrences(tmp_path):
    first = progress("a1", "spawn", tokens=3, uuid="absolute-id",
                     content=[{"type": "text", "text": "first copy"}])
    repeated = progress("a1", "spawn", tokens=9, uuid="absolute-id",
                        content=[{"type": "text", "text": "second copy"}])
    p = tmp_path / "session.jsonl"
    write_jsonl(p, [first, repeated])
    sub = tmp_path / "session" / "subagents"
    os.makedirs(sub)
    write_jsonl(sub / "agent-a1.jsonl", [
        {"uuid": "absolute-id", "timestamp": "2026-06-29T10:00:05",
         "message": first["data"]["message"]["message"]},
    ])
    m = ClaudeModel(str(p))
    m.poll()

    assert m.nodes["a1"].tokens == 3
    texts = [step for step in m.nodes["a1"].steps if step.kind == "text"]
    assert len(texts) == 1
    assert texts[0].body == "first copy"


def test_flat_agent_tail_waits_for_complete_record_then_appends_once(tmp_path):
    p = tmp_path / "session.jsonl"
    write_jsonl(p, [])
    sub = tmp_path / "session" / "subagents"
    os.makedirs(sub)
    fp = sub / "agent-a1.jsonl"
    record = {"uuid": "record-1", "timestamp": "2026-06-29T10:00:01",
              "unused": "x" * 100,
              "message": {"role": "assistant", "model": "claude-sonnet-4-6",
                          "usage": {"output_tokens": 5},
                          "content": [{"type": "text", "text": "first"}]}}
    encoded = json.dumps(record)
    fp.write_text(encoded[:len(encoded) // 2])
    m = ClaudeModel(str(p))
    assert m.poll() is False
    assert "a1" not in m.nodes

    with open(fp, "a") as f:
        f.write(encoded[len(encoded) // 2:] + "\n")
    m.poll()
    assert m.nodes["a1"].tokens == 5
    assert len(m.nodes["a1"].steps) == 1

    second = {"uuid": "record-2", "timestamp": "2026-06-29T10:00:02",
              "message": {"role": "assistant", "usage": {"output_tokens": 3},
                          "content": [{"type": "text", "text": "second"}]}}
    append_jsonl(fp, [second])
    m.poll()
    assert m.nodes["a1"].tokens == 8
    assert len(m.nodes["a1"].steps) == 2


def test_same_inode_main_and_auxiliary_rewrites_freeze_projection(tmp_path):
    path = tmp_path / "session.jsonl"
    write_jsonl(path, [root_text("main")])
    child_dir = tmp_path / "session" / "subagents"
    child_dir.mkdir(parents=True)
    child = child_dir / "agent-a1.jsonl"
    write_jsonl(child, [flat_child_record("old", "old child")])
    session = ClaudeModel(str(path))
    while session.poll():
        pass
    expected = public_projection(session)

    write_jsonl(path, [root_text("replacement main", "x" * 256)])
    write_jsonl(child, [flat_child_record("new", "replacement child")])

    assert session.poll() is False
    assert public_projection(session) == expected


def bg_bash_turn(tool_id, command="npm run dev", ts="2026-06-29T10:00:00"):
    return {
        "type": "assistant",
        "timestamp": ts,
        "message": {
            "timestamp": ts,
            "content": [{
                "type": "tool_use", "id": tool_id, "name": "Bash",
                "input": {"command": command, "run_in_background": True},
            }],
        },
    }


def bg_ack(tool_id, out_path, shell_id="bnaauwrzm", ts="2026-06-29T10:00:01"):
    text = (
        f"Command running in background with ID: {shell_id}. "
        f"Output is being written to: {out_path}"
    )
    content = {
        "type": "tool_result", "tool_use_id": tool_id, "content": text,
    }
    return {"type": "user", "timestamp": ts, "message": {"content": [content]}}


def test_background_shell_stays_running_and_tails_output(tmp_path):
    out = tmp_path / "shell.output"
    out.write_text("compiling...\n")
    p = tmp_path / "session.jsonl"
    write_jsonl(p, [bg_bash_turn("tool_1"), bg_ack("tool_1", str(out))])
    m = ClaudeModel(str(p))
    m.poll()

    step = m.nodes["main"].steps[-1]
    assert step.kind == "tool"
    assert step.bg_path == str(out)
    # The launch ack must NOT mark it done — the command is still running.
    assert step.status == "running"
    assert "compiling" in step.output

    # New output appended to the file is tailed into the step on the next poll.
    with open(out, "a") as f:
        f.write("listening on :3000\n")
    assert m.poll() is True
    assert "listening on :3000" in step.output


@pytest.mark.parametrize("output_state", ["missing", "oversized"])
def test_unavailable_background_output_does_not_block_main_append(
        tmp_path, output_state):
    out = tmp_path / "shell.output"
    out.write_text("initial output\n")
    path = tmp_path / "session.jsonl"
    write_jsonl(path, [bg_bash_turn("tool_1"), bg_ack("tool_1", str(out))])
    session = ClaudeModel(str(path))
    while session.poll():
        pass
    if output_state == "missing":
        out.unlink()
    else:
        with out.open("wb") as stream:
            stream.truncate(JSONL_RECORD_LIMIT + 1)
    append_jsonl(path, [root_text("main suffix")])

    assert session.poll() is True
    assert any(
        step.kind == "text" and step.body == "main suffix"
        for step in session.nodes["main"].steps
    )
    assert session.poll() is False


def test_missing_background_output_is_not_a_snapshot_source_claim(tmp_path):
    output = tmp_path / "shell.output"
    output.write_text("initial output\n")
    path = tmp_path / "session.jsonl"
    write_jsonl(path, [
        bg_bash_turn("tool_1"), bg_ack("tool_1", str(output)),
    ])
    session = ClaudeModel(str(path))
    while session.poll():
        pass
    output.unlink()

    snapshot = ClaudeSnapshotCodec().capture(session)

    missing = os.path.realpath(output)
    assert not any(
        claim.kind == "file" and claim.path == missing
        for claim in snapshot.sources
    )


def test_background_output_append_advances_public_activity_timestamp(tmp_path):
    out = tmp_path / "shell.output"
    out.write_text("initial output\n")
    path = tmp_path / "session.jsonl"
    write_jsonl(path, [bg_bash_turn("tool_1"), bg_ack("tool_1", str(out))])
    session = ClaudeModel(str(path))
    while session.poll():
        pass
    step = next(
        step for step in session.nodes["main"].steps
        if step.bg_path == str(out) and step.status == "running"
    )
    before = step.ts
    activity_at = before + 60

    with out.open("a") as stream:
        stream.write("new output\n")
    os.utime(out, (activity_at, activity_at))

    assert session.poll() is True
    assert step.ts > before
    assert step.ts == pytest.approx(activity_at)
    assert session.nodes["main"].last_ts == pytest.approx(activity_at)


def test_background_shell_same_inode_rewrite_freezes_old_output(tmp_path):
    out = tmp_path / "shell.output"
    out.write_text("old output\n")
    path = tmp_path / "session.jsonl"
    write_jsonl(path, [bg_bash_turn("tool_1"), bg_ack("tool_1", str(out))])
    session = ClaudeModel(str(path))
    session.poll()
    session.poll()
    step = session.nodes["main"].steps[-1]
    inode = out.stat().st_ino
    assert step.output == "old output\n"

    out.write_text("new output\n")
    assert out.stat().st_ino == inode
    assert session.poll() is False
    step = session.nodes["main"].steps[-1]
    assert step.output == "old output\n"
    assert session.poll() is False


def test_background_shell_never_falsely_marked_done_when_quiet(tmp_path):
    # A backgrounded process (e.g. an idle dev server) can go quiet for a long time
    # while still very much alive, and Claude Code emits NO event when it exits — so
    # silence is not completion. The step must stay "running" (reads as idle), never
    # flip to a false "done".
    out = tmp_path / "shell.output"
    out.write_text("listening on :3000\n")
    p = tmp_path / "session.jsonl"
    write_jsonl(p, [bg_bash_turn("tool_1"), bg_ack("tool_1", str(out))])
    m = ClaudeModel(str(p))
    m.poll()
    step = m.nodes["main"].steps[-1]
    assert step.status == "running"

    # Age the output file well past any idle window and poll repeatedly.
    old = time.time() - 600
    os.utime(out, (old, old))
    for _ in range(3):
        m.poll()
    assert step.status == "running"   # quiet != done; completion is unknowable


# ---- workflow store --------------------------------------------------------

def test_workflow_run_builds_subtree_and_completes(tmp_path):
    p = tmp_path / "session.jsonl"
    write_jsonl(p, [])
    wf = tmp_path / "session" / "subagents" / "workflows" / "wf_run1"
    os.makedirs(wf)
    write_jsonl(wf / "journal.jsonl", [
        {"type": "started", "agentId": "w1"},
        {"type": "result", "agentId": "w1", "result": "all good"},
    ])
    write_jsonl(wf / "agent-w1.jsonl", [
        {"timestamp": "2026-06-29T10:00:01",
         "message": {"role": "user", "content": "scan the repo"}},
        {"timestamp": "2026-06-29T10:00:02",
         "message": {"role": "assistant", "model": "claude-opus-4-8",
                     "usage": {"output_tokens": 7},
                     "content": [{"type": "text", "text": "scanning"}]}},
    ])
    m = ClaudeModel(str(p))
    m.poll()

    assert "wf:wf_run1" in m.nodes
    wnode = m.nodes["wf:wf_run1"]
    assert wnode.kind == "workflow"
    assert "wfa:wf_run1:w1" in m.nodes
    child = m.nodes["wfa:wf_run1:w1"]
    assert child.parent == "wf:wf_run1"
    assert child.status == "done"
    assert child.result == "all good"
    assert child.model == "opus"
    assert wnode.status == "done"               # all children done -> workflow done


@pytest.mark.parametrize("invalid_field", ["agentId", "result"])
def test_claude_workflow_append_requires_exact_event_field_types(
        tmp_path, invalid_field):
    path = tmp_path / "session.jsonl"
    write_jsonl(path, [])
    workflow_dir = tmp_path / "session" / "subagents" / "workflows" / "wf_run1"
    workflow_dir.mkdir(parents=True)
    journal = workflow_dir / "journal.jsonl"
    write_jsonl(journal, [
        {"type": "started", "agentId": "w1"},
        {"type": "result", "agentId": "w1", "result": "old result"},
    ])
    session = ClaudeModel(str(path))
    session.poll()
    session.poll()
    expected = public_projection(session)

    invalid_result = {
        "type": "result", "agentId": "w1", "result": "new result",
    }
    invalid_result[invalid_field] = {"wrong": "type"}
    append_jsonl(journal, [invalid_result])
    assert session.poll() is False
    assert public_projection(session) == expected
    assert session.poll() is False
    assert public_projection(session) == expected



def test_completed_workflow_reopens_when_a_new_agent_starts(tmp_path):
    p = tmp_path / "session.jsonl"
    write_jsonl(p, [])
    wf = tmp_path / "session" / "subagents" / "workflows" / "wf_run1"
    os.makedirs(wf)
    journal = wf / "journal.jsonl"
    write_jsonl(journal, [
        {"type": "started", "agentId": "w1"},
        {"type": "result", "agentId": "w1", "result": "done"},
    ])
    m = ClaudeModel(str(p))
    m.poll()
    assert m.nodes["wf:wf_run1"].status == "done"

    append_jsonl(journal, [{"type": "started", "agentId": "w1"}])
    m.poll()
    assert m.nodes["wfa:wf_run1:w1"].status == "running"
    assert m.nodes["wfa:wf_run1:w1"].result == ""
    assert m.nodes["wf:wf_run1"].status == "running"


# ---- team teammates --------------------------------------------------------

def _team_member_meta(team="session-xyz", name="alice"):
    return {"taskKind": "in_process_teammate", "teamName": team, "name": name,
            "model": "claude-opus-4-8", "agentType": "general-purpose",
            "description": "do team work"}


def _setup_teammate(tmp_path, name="alice"):
    p = tmp_path / "session.jsonl"
    write_jsonl(p, [])
    sub = tmp_path / "session" / "subagents"
    os.makedirs(sub)
    write_jsonl(sub / "agent-t1.jsonl", [
        {"timestamp": "2026-06-29T10:00:01",
         "message": {"role": "assistant", "model": "claude-opus-4-8",
                     "usage": {"output_tokens": 5},
                     "content": [{"type": "text", "text": "hi"}]}},
    ])
    with open(sub / "agent-t1.meta.json", "w") as f:
        json.dump(_team_member_meta(name=name), f)
    return p


def _setup_team_task(tmp_path, monkeypatch):
    teams = tmp_path / "teams"
    tasks = tmp_path / "tasks"
    monkeypatch.setattr(model, "TEAMS", str(teams))
    monkeypatch.setattr(model, "TASKS", str(tasks))
    path = _setup_teammate(tmp_path)
    config_dir = teams / "session-xyz"
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text(json.dumps({
        "members": [
            {"name": "alice", "agentType": "general-purpose"},
            {"name": "team-lead", "agentType": "team-lead"},
        ],
    }))
    task_dir = tasks / "session-xyz"
    task_dir.mkdir(parents=True)
    task_path = task_dir / "1.json"
    task_path.write_text(json.dumps({
        "id": "1",
        "subject": "Review",
        "status": "in_progress",
        "owner": "alice",
        "description": "old task",
    }))
    return path, task_path


def test_claude_team_and_task_directories_support_symlinks(
        tmp_path, monkeypatch):
    path, _ = _setup_team_task(tmp_path, monkeypatch)
    direct = ClaudeModel(str(path))
    while direct.poll():
        pass
    expected = public_projection(direct)
    aliases = tmp_path / "aliases"
    aliases.mkdir()
    teams = aliases / "teams"
    tasks = aliases / "tasks"
    teams.symlink_to(tmp_path / "teams", target_is_directory=True)
    tasks.symlink_to(tmp_path / "tasks", target_is_directory=True)
    monkeypatch.setattr(model, "TEAMS", str(teams))
    monkeypatch.setattr(model, "TASKS", str(tasks))

    session = ClaudeModel(str(path))
    while session.poll():
        pass

    assert public_projection(session) == expected
    assert session.nodes["tm:session-xyz:t1"].status == "running"
    task = session.task_list["session-xyz"][0]
    assert (task.id, task.subject, task.status, task.owner) == (
        "1", "Review", "in_progress", "alice",
    )
    assert session.nodes["task:session-xyz:1"].status == "in_progress"


def test_teammate_running_while_in_roster(tmp_path, monkeypatch):
    teams = tmp_path / "teams"
    monkeypatch.setattr(model, "TEAMS", str(teams))
    p = _setup_teammate(tmp_path)
    cfg_dir = teams / "session-xyz"
    os.makedirs(cfg_dir)
    with open(cfg_dir / "config.json", "w") as f:
        json.dump({"members": [{"name": "alice", "agentType": "general-purpose"},
                               {"name": "team-lead", "agentType": "team-lead"}]}, f)

    m = ClaudeModel(str(p))
    m.poll()
    assert "team:session-xyz" in m.nodes
    assert m.nodes["team:session-xyz"].kind == "team"
    mate = m.nodes["tm:session-xyz:t1"]
    assert mate.label == "alice"
    assert mate.model == "opus"
    assert mate.status == "running"             # still listed in members


def test_teammate_done_when_pruned_collapses_team(tmp_path, monkeypatch):
    teams = tmp_path / "teams"
    monkeypatch.setattr(model, "TEAMS", str(teams))
    p = _setup_teammate(tmp_path)
    cfg_dir = teams / "session-xyz"
    os.makedirs(cfg_dir)
    with open(cfg_dir / "config.json", "w") as f:
        json.dump({"members": [{"name": "team-lead", "agentType": "team-lead"}]}, f)

    m = ClaudeModel(str(p))
    m.poll()
    assert m.nodes["tm:session-xyz:t1"].status == "done"   # no longer in roster
    assert m.nodes["team:session-xyz"].status == "done"    # all members done


def test_teammate_stays_running_while_quiet_but_listed(tmp_path, monkeypatch):
    # config.json still lists alice (roster not pruned) but both the teammate's
    # transcript and the lead session have gone quiet. Silence is NOT completion —
    # the teammate may be waiting on a lock or a slow tool — so it stays running.
    # Only a roster drop marks it done.
    teams = tmp_path / "teams"
    monkeypatch.setattr(model, "TEAMS", str(teams))
    p = _setup_teammate(tmp_path)
    cfg_dir = teams / "session-xyz"
    os.makedirs(cfg_dir)
    with open(cfg_dir / "config.json", "w") as f:
        json.dump({"members": [{"name": "alice", "agentType": "general-purpose"},
                               {"name": "team-lead", "agentType": "team-lead"}]}, f)
    mate_fp = tmp_path / "session" / "subagents" / "agent-t1.jsonl"
    old = time.time() - 600
    os.utime(mate_fp, (old, old))
    os.utime(p, (old, old))

    m = ClaudeModel(str(p))
    m.poll()
    assert m.nodes["tm:session-xyz:t1"].status == "running"   # quiet != done, still listed


def test_teammate_running_while_lead_busy(tmp_path, monkeypatch):
    # Teammate transcript is idle but still on the roster -> merely waiting, running.
    teams = tmp_path / "teams"
    monkeypatch.setattr(model, "TEAMS", str(teams))
    p = _setup_teammate(tmp_path)
    cfg_dir = teams / "session-xyz"
    os.makedirs(cfg_dir)
    with open(cfg_dir / "config.json", "w") as f:
        json.dump({"members": [{"name": "alice", "agentType": "general-purpose"},
                               {"name": "team-lead", "agentType": "team-lead"}]}, f)
    mate_fp = tmp_path / "session" / "subagents" / "agent-t1.jsonl"
    old = time.time() - 120
    os.utime(mate_fp, (old, old))          # teammate quiet, but leave p (lead) fresh

    m = ClaudeModel(str(p))
    m.poll()
    assert m.nodes["tm:session-xyz:t1"].status == "running"


def test_late_teammate_metadata_reclassifies_provisional_node(tmp_path, monkeypatch):
    teams = tmp_path / "teams"
    monkeypatch.setattr(model, "TEAMS", str(teams))
    p = tmp_path / "session.jsonl"
    write_jsonl(p, [])
    sub = tmp_path / "session" / "subagents"
    os.makedirs(sub)
    write_jsonl(sub / "agent-t1.jsonl", [
        {"uuid": "record-1", "timestamp": "2026-06-29T10:00:01",
         "message": {"role": "assistant", "model": "claude-opus-4-8",
                     "usage": {"output_tokens": 5},
                     "content": [{"type": "text", "text": "hi"}]}},
    ])
    meta_fp = sub / "agent-t1.meta.json"
    meta_fp.write_text('{"taskKind":')
    cfg_dir = teams / "session-xyz"
    os.makedirs(cfg_dir)
    with open(cfg_dir / "config.json", "w") as f:
        json.dump({"members": [{"name": "alice", "agentType": "general-purpose"}]}, f)

    m = ClaudeModel(str(p))
    assert m.poll() is False
    assert set(m.nodes) == {"main"}

    with open(meta_fp, "w") as f:
        json.dump(_team_member_meta(), f)
    assert m.poll() is True
    mate = m.nodes["tm:session-xyz:t1"]
    assert mate.label == "alice"
    assert mate.tokens == 5
    assert len([step for step in mate.steps if step.kind == "text"]) == 1


def test_late_workflow_metadata_drops_stale_spawn_references_safely(tmp_path):
    nested_use = {"type": "tool_use", "id": "nested-spawn", "name": "Task",
                  "input": {"subagent_type": "general-purpose", "prompt": "nested"}}
    root_progress = progress("root-agent", "outer-spawn", uuid="root-message",
                             tokens=1, content=[nested_use])
    p = tmp_path / "session.jsonl"
    write_jsonl(p, [main_turn("outer-spawn"), root_progress])
    sub = tmp_path / "session" / "subagents"
    os.makedirs(sub)
    write_jsonl(sub / "agent-root-agent.jsonl", [
        {"uuid": "root-message", "timestamp": "2026-06-29T10:00:05",
         "message": root_progress["data"]["message"]["message"]},
    ])
    meta_fp = sub / "agent-root-agent.meta.json"
    m = ClaudeModel(str(p))
    m.poll()
    assert m._spawn["outer-spawn"] == "root-agent"
    assert m._spawn_owner["nested-spawn"] == "root-agent"

    with open(meta_fp, "w") as f:
        json.dump({"agentType": "workflow-subagent"}, f)
    assert m.poll() is True
    assert "root-agent" not in m.nodes
    assert "outer-spawn" not in m._spawn
    assert "nested-spawn" not in m._spawn_owner
    assert m._step_by_tid["outer-spawn"].child == ""
    assert all(step.child != "root-agent" for step in m._step_by_tid.values())

    child_progress = progress("nested-child", "nested-spawn", uuid="child-message", tokens=2)
    child_progress["parentToolUseID"] = "nested-spawn"
    append_jsonl(p, [child_progress])
    assert m.poll() is True
    assert m.nodes["nested-child"].parent == "main"


def test_claude_accepted_metadata_edit_freezes_projection(tmp_path):
    path = tmp_path / "session.jsonl"
    write_jsonl(path, [root_text("main")])
    child_dir = tmp_path / "session" / "subagents"
    child_dir.mkdir(parents=True)
    write_jsonl(child_dir / "agent-child.jsonl", [
        flat_child_record("child", "child work"),
    ])
    metadata = child_dir / "agent-child.meta.json"
    metadata.write_text(json.dumps({"agentType": "researcher"}))
    session = ClaudeModel(str(path))
    while session.poll():
        pass
    expected = public_projection(session)

    metadata.write_text(json.dumps({"agentType": "reviewer"}))

    assert session.poll() is False
    assert public_projection(session) == expected


@pytest.mark.parametrize(
    "invalid_field",
    [
        "agentType", "description", "taskKind", "teamName", "name", "model",
        "toolUseId",
    ],
)
def test_claude_new_flat_metadata_requires_exact_types(
        tmp_path, invalid_field):
    path = tmp_path / "session.jsonl"
    write_jsonl(path, [root_text("main")])
    child_dir = tmp_path / "session" / "subagents"
    child_dir.mkdir(parents=True)
    write_jsonl(child_dir / "agent-child.jsonl", [
        flat_child_record("child", "child work"),
    ])
    session = ClaudeModel(str(path))
    while session.poll():
        pass
    expected = public_projection(session)
    invalid = {"agentType": "researcher", invalid_field: ["wrong", "type"]}

    (child_dir / "agent-child.meta.json").write_text(json.dumps(invalid))

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
def test_claude_new_metadata_rejects_non_finite_values(tmp_path, non_finite):
    path = tmp_path / "session.jsonl"
    write_jsonl(path, [root_text("main")])
    child_dir = tmp_path / "session" / "subagents"
    child_dir.mkdir(parents=True)
    write_jsonl(child_dir / "agent-child.jsonl", [
        flat_child_record("child", "child work"),
    ])
    session = ClaudeModel(str(path))
    while session.poll():
        pass
    expected = public_projection(session)

    (child_dir / "agent-child.meta.json").write_text(json.dumps({
        "agentType": "private-agent",
        "future": {"scores": [non_finite]},
    }))

    assert session.poll() is False
    assert public_projection(session) == expected


def test_claude_accepted_team_config_edit_freezes_status(tmp_path, monkeypatch):
    teams = tmp_path / "teams"
    monkeypatch.setattr(model, "TEAMS", str(teams))
    path = _setup_teammate(tmp_path)
    config_dir = teams / "session-xyz"
    config_dir.mkdir(parents=True)
    config = config_dir / "config.json"
    config.write_text(json.dumps({
        "members": [{"name": "team-lead", "agentType": "team-lead"}],
    }))
    session = ClaudeModel(str(path))
    while session.poll():
        pass
    expected = public_projection(session)

    config.write_text(json.dumps({
        "members": [{"name": "alice", "agentType": "general-purpose"}],
    }))

    assert session.poll() is False
    assert public_projection(session) == expected


@pytest.mark.parametrize("invalid_field", ["name", "agentType"])
def test_claude_new_team_config_requires_exact_member_types(
        tmp_path, monkeypatch, invalid_field):
    teams = tmp_path / "teams"
    monkeypatch.setattr(model, "TEAMS", str(teams))
    path = _setup_teammate(tmp_path)
    session = ClaudeModel(str(path))
    while session.poll():
        pass
    expected = public_projection(session)
    member = {"name": "alice", "agentType": "general-purpose"}
    member[invalid_field] = 7
    config_dir = teams / "session-xyz"
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text(json.dumps({"members": [member]}))

    assert session.poll() is False
    assert public_projection(session) == expected


@pytest.mark.parametrize("change", ["edit", "delete", "directory"])
def test_claude_accepted_task_change_freezes_projection(
        tmp_path, monkeypatch, change):
    path, task = _setup_team_task(tmp_path, monkeypatch)
    session = ClaudeModel(str(path))
    while session.poll():
        pass
    expected = public_projection(session)

    if change == "edit":
        task.write_text(json.dumps({
            "id": "1", "subject": "Review", "status": "done",
            "owner": "alice", "description": "replacement",
        }))
    else:
        task.unlink()
        if change == "directory":
            task.mkdir()

    assert session.poll() is False
    assert public_projection(session) == expected


@pytest.mark.parametrize(
    "invalid_field", ["id", "subject", "status", "owner", "description"],
)
def test_claude_new_task_requires_exact_field_types(
        tmp_path, monkeypatch, invalid_field):
    teams = tmp_path / "teams"
    tasks = tmp_path / "tasks"
    monkeypatch.setattr(model, "TEAMS", str(teams))
    monkeypatch.setattr(model, "TASKS", str(tasks))
    path = _setup_teammate(tmp_path)
    config_dir = teams / "session-xyz"
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text(json.dumps({
        "members": [{"name": "alice", "agentType": "general-purpose"}],
    }))
    session = ClaudeModel(str(path))
    while session.poll():
        pass
    expected = public_projection(session)
    invalid = {
        "id": "1", "subject": "Review", "status": "pending",
        "owner": "alice", "description": "private",
    }
    invalid[invalid_field] = {"wrong": "type"}
    task_dir = tasks / "session-xyz"
    task_dir.mkdir(parents=True)
    (task_dir / "1.json").write_text(json.dumps(invalid))

    assert session.poll() is False
    assert public_projection(session) == expected


def test_claude_new_task_path_applies_once_then_freezes(tmp_path, monkeypatch):
    path, first = _setup_team_task(tmp_path, monkeypatch)
    session = ClaudeModel(str(path))
    while session.poll():
        pass
    second = first.with_name("2.json")
    second.write_text(json.dumps({
        "id": "2", "subject": "New", "status": "pending",
        "owner": "alice", "description": "first value",
    }))

    assert session.poll() is True
    assert [task.id for task in session.task_list["session-xyz"]] == ["1", "2"]
    expected = public_projection(session)
    second.write_text(json.dumps({
        "id": "2", "subject": "New", "status": "done",
        "owner": "alice", "description": "edited value",
    }))
    assert session.poll() is False
    assert public_projection(session) == expected


def test_text_of_handles_str_list_and_nested_tool_result():
    assert _text_of("plain") == "plain"
    assert _text_of([{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]) == "a\nb"
    nested = [{"type": "tool_result", "content": [{"type": "text", "text": "inner"}]}]
    assert _text_of(nested) == "inner"
    assert _text_of(123) == ""


def test_ts_parses_utc_and_tolerates_garbage():
    # Parsed as UTC (not local), monotonic, and the trailing "Z" is ignored.
    t0 = parse_timestamp("2026-06-29T10:00:00Z")
    t1 = parse_timestamp("2026-06-29T10:00:10Z")
    assert t1 - t0 == 10.0
    assert parse_timestamp("2026-06-29T10:00:00") == t0      # Z is optional
    assert parse_timestamp(None) == 0.0
    assert parse_timestamp("not-a-date") == 0.0


# ---- skill awareness -------------------------------------------------------

def tool_turn(blocks, ts="2026-06-29T10:00:00"):
    return {"type": "assistant", "timestamp": ts,
            "message": {"timestamp": ts, "usage": {"output_tokens": 1},
                        "content": blocks}}


def test_skill_step_title_carries_name(tmp_path):
    p = tmp_path / "session.jsonl"
    write_jsonl(p, [tool_turn([
        {"type": "tool_use", "id": "t1", "name": "Skill",
         "input": {"skill": "development-turn", "args": ""}},
    ])])
    m = ClaudeModel(str(p))
    m.poll()
    titles = [s.title for s in m.nodes["main"].steps if s.kind == "tool"]
    assert "Skill: development-turn" in titles
    assert m.nodes["main"].skills == ["development-turn"]
