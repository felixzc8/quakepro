import builtins
import copy
import json
import os

import pytest

import quakepro.skill_mode as skill_mode
import pi_fixture
import quakepro.provider_reload as provider_reload
from quakepro.graph import render_tree
from quakepro.pi_model import PiModel, newest_pi_session, pi_session_dir, pi_session_slug
from quakepro.pi_snapshot import PiSnapshotCodec
from quakepro.session_identity import detect_provider
from quakepro.sessions import open_model


def session(tmp_path, lines=None, name="session.jsonl"):
    return pi_fixture.write_session(tmp_path / name,
                                    lines if lines is not None else pi_fixture.entries())


def poll_all(model):
    for _ in range(16):
        if not model.poll():
            return
    raise AssertionError("pi model did not settle")


def async_fixture(tmp_path, *, state="running", child_status="running",
                  child_count=1, session_id=None, include_child_run_id=True,
                  agent="reviewer"):
    run_id = "run-async-1"
    run_dir = tmp_path / "pi-subagents-test" / "async-subagent-runs" / run_id
    run_dir.mkdir(parents=True)
    parent = tmp_path / "session.jsonl"
    children = []
    steps = []
    for index in range(child_count):
        child_run = f"child-{index + 1}"
        child_path = tmp_path / parent.stem / child_run / "run-0" / "session.jsonl"
        child_path.parent.mkdir(parents=True)
        pi_fixture.write_session(child_path, [
            pi_fixture.header(cwd=str(tmp_path), session_id=f"session-{index + 1}"),
            pi_fixture.message(f"prompt-{index}", None, pi_fixture.user(f"Task {index + 1}")),
            pi_fixture.message(
                f"reply-{index}", f"prompt-{index}",
                pi_fixture.assistant([
                    {"type": "thinking", "thinking": f"Plan {index + 1}"},
                    pi_fixture.tool_call(f"read-{index}", "read", {"path": "README.md"}),
                ], stop_reason="toolUse", model="gpt-5.6-sol", output=7),
            ),
        ])
        children.append(child_path)
        step = {
            "agent": agent,
            "label": f"review-{index + 1}",
            "workflowKey": f"review-{index + 1}",
            "status": child_status,
            "sessionFile": str(child_path),
            "model": "gpt-5.6-sol",
        }
        if include_child_run_id:
            step["runId"] = child_run
        steps.append(step)
    details = {
        "mode": "workflow",
        "runId": run_id,
        "asyncId": run_id,
        "asyncDir": str(run_dir),
        "results": [],
    }
    lines = [
        pi_fixture.header(cwd=str(tmp_path)),
        pi_fixture.message("prompt", None, pi_fixture.user("Run reviews")),
        pi_fixture.message("call", "prompt", pi_fixture.assistant([
            pi_fixture.tool_call("call-async", "subagent", {
                "workflowScript": "return runs.all([])",
            }),
        ], stop_reason="toolUse")),
        pi_fixture.message(
            "result", "call",
            pi_fixture.tool_result("call-async", "subagent", "Detached", details=details),
        ),
    ]
    pi_fixture.write_session(parent, lines)
    status = {
        "lifecycleArtifactVersion": 3,
        "runId": run_id,
        "sessionId": session_id or str(parent),
        "mode": "workflow",
        "state": state,
        "asyncDir": str(run_dir),
        "steps": steps,
        "futureField": {"ignored": True},
    }
    (run_dir / "status.json").write_text(json.dumps(status), encoding="utf-8")
    (run_dir / "events.jsonl").write_text(
        json.dumps({"type": "subagent.run.started", "runId": run_id}) + "\n",
        encoding="utf-8",
    )
    return parent, run_dir, children, status, lines


def public_projection(session):
    return copy.deepcopy((
        session.lifecycle_identity(),
        session.nodes,
        session.task_list,
    ))


def nested_value(containers):
    value = 0
    for _ in range(containers):
        value = [value]
    return value


def rewrite_after_pi_header_capture(monkeypatch, path, replacement):
    original_open = builtins.open
    target_path = os.path.realpath(path)
    rewritten = []

    class HeaderReader:
        def __init__(self, wrapped):
            self.wrapped = wrapped
            self.captured = False

        def __enter__(self):
            self.wrapped.__enter__()
            return self

        def __exit__(self, *args):
            result = self.wrapped.__exit__(*args)
            if self.captured and not rewritten:
                with original_open(path, "wb") as output:
                    output.write(replacement)
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
        return HeaderReader(wrapped) if candidate_path == target_path else wrapped

    monkeypatch.setattr(provider_reload, "open", open_then_rewrite, raising=False)
    return rewritten


def rewrite_entries(text, padding="", session_id=pi_fixture.SESSION_ID):
    header = pi_fixture.header(session_id=session_id)
    header["padding"] = padding
    return [
        header,
        pi_fixture.message(
            "rewrite-message",
            None,
            pi_fixture.assistant([{"type": "text", "text": text}]),
        ),
    ]


def pi_semantic_replacement(case):
    header = pi_fixture.header()
    partial = rewrite_entries("partial new")[1]
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
            name = "bash"
        elif field == "patch":
            name = "apply_patch"
        elif field in {
            "prompt", "message", "task", "description", "instructions",
            "task_name",
        }:
            name = "subagent"
        else:
            name = "edit"
        invalid = pi_fixture.message(
            "recognizer-call",
            None,
            pi_fixture.assistant([
                pi_fixture.tool_call("recognizer-call", name, {field: value}),
            ], stop_reason="toolUse"),
        )
        return [header, partial, invalid]
    if case.startswith("header-"):
        field = case.removeprefix("header-")
        header[field] = "3" if field == "version" else 7
        return [header, partial]
    if case == "entry-timestamp":
        invalid = pi_fixture.message(
            "typed-field", None,
            pi_fixture.assistant([{"type": "text", "text": "typed field"}]),
        )
        invalid["timestamp"] = 7
    elif case in {
        "assistant-thinking", "assistant-block-type", "assistant-usage",
        "assistant-output-tokens", "assistant-model", "assistant-stop-reason",
        "assistant-error-message",
    }:
        message = pi_fixture.assistant([{"type": "thinking", "thinking": "plan"}])
        if case == "assistant-thinking":
            message["content"][0]["thinking"] = 7
        elif case == "assistant-block-type":
            message["content"][0]["type"] = 7
        elif case == "assistant-usage":
            message["usage"] = ["not", "an", "object"]
        elif case == "assistant-output-tokens":
            message["usage"]["output"] = "120"
        elif case == "assistant-model":
            message["model"] = 7
        elif case == "assistant-stop-reason":
            message["stopReason"] = 7
        else:
            message["stopReason"] = "error"
            message["errorMessage"] = 7
        invalid = pi_fixture.message("typed-field", None, message)
    elif case.startswith("tool-call-"):
        block = pi_fixture.tool_call(
            "call-1", "subagent", {"agent": "researcher", "task": "inspect"},
        )
        field = case.removeprefix("tool-call-")
        if field == "arguments":
            block["arguments"] = ["not", "an", "object"]
        elif field == "task":
            block["arguments"]["task"] = 7
        else:
            block[field] = 7
        invalid = pi_fixture.message(
            "typed-field", None, pi_fixture.assistant([block], stop_reason="toolUse"),
        )
    elif case.startswith("tool-result-"):
        call = pi_fixture.message(
            "tool-call", None,
            pi_fixture.assistant([
                pi_fixture.tool_call("call-1", "bash", {"command": "pwd"}),
            ], stop_reason="toolUse"),
        )
        result = pi_fixture.tool_result("call-1", "bash", "done")
        field = case.removeprefix("tool-result-")
        if field == "toolCallId":
            result[field] = 7
        elif field == "content":
            result[field] = [{"type": "text", "text": 7}]
        else:
            result[field] = "false"
        return [header, partial, call, pi_fixture.message("tool-result", "tool-call", result)]
    elif case.startswith("bash-"):
        message = {
            "role": "bashExecution", "command": "pwd", "output": "/work",
            "exitCode": 0, "cancelled": False,
        }
        field = case.removeprefix("bash-")
        message[field] = "0" if field == "exitCode" else ("false" if field == "cancelled" else 7)
        invalid = pi_fixture.message("typed-field", None, message)
    elif case == "user-content":
        invalid = pi_fixture.message("typed-field", None, {"role": "user", "content": 7})
    elif case.startswith("custom-"):
        message = {"role": "custom", "display": True, "content": "notice"}
        field = case.removeprefix("custom-")
        message[field] = "true" if field == "display" else 7
        invalid = pi_fixture.message("typed-field", None, message)
    else:
        records = copy.deepcopy(pi_fixture.subagent_entries())
        result = records[-1]["message"]["details"]["results"][0]
        field = case.removeprefix("child-")
        if field == "messages":
            result[field] = {"not": "a list"}
        elif field == "exitCode":
            result[field] = "0"
        elif field == "thinking":
            result["messages"] = [pi_fixture.assistant([
                {"type": "thinking", "thinking": 7},
            ])]
        elif field == "text":
            result["messages"] = [pi_fixture.assistant([
                {"type": "text", "text": 7},
            ])]
        else:
            result[field] = 7
        return [header, partial, *records[1:]]
    return [header, partial, invalid]


@pytest.mark.parametrize(
    ("old_padding", "new_padding"),
    [("x" * 256, ""), ("x" * 32, "y" * 32), ("", "y" * 256)],
)
def test_pi_accepted_rewrite_freezes_projection(
        tmp_path, old_padding, new_padding):
    path = session(tmp_path, rewrite_entries("old value", old_padding))
    model = PiModel(path)
    while model.poll():
        pass
    expected = public_projection(model)

    pi_fixture.write_session(path, rewrite_entries("new value", new_padding))

    assert model.poll() is False
    assert public_projection(model) == expected


@pytest.mark.parametrize(
    "case",
    [
        "header-version",
        "header-timestamp",
        "entry-timestamp",
        "assistant-thinking",
        "assistant-block-type",
        "assistant-usage",
        "assistant-output-tokens",
        "assistant-model",
        "assistant-stop-reason",
        "assistant-error-message",
        "tool-call-id",
        "tool-call-name",
        "tool-call-arguments",
        "tool-call-task",
        "tool-result-toolCallId",
        "tool-result-content",
        "tool-result-isError",
        "bash-command",
        "bash-output",
        "bash-exitCode",
        "bash-cancelled",
        "user-content",
        "custom-display",
        "custom-content",
        "child-agent",
        "child-task",
        "child-stopReason",
        "child-model",
        "child-exitCode",
        "child-messages",
        "child-thinking",
        "child-text",
        "recognizer-input-skill",
        "recognizer-input-file_path",
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
        "recognizer-input-prompt",
        "recognizer-input-message",
        "recognizer-input-description",
        "recognizer-input-instructions",
        "recognizer-input-task_name",
        "recognizer-input-edits",
        "recognizer-input-edits-item",
    ],
)
def test_pi_append_requires_exact_types_for_graph_fields(tmp_path, case):
    path = session(tmp_path, rewrite_entries("old value"))
    model = PiModel(path)
    while model.poll():
        pass
    expected = public_projection(model)

    with open(path, "a", encoding="utf-8") as stream:
        for entry in pi_semantic_replacement(case):
            stream.write(json.dumps(entry) + "\n")

    assert model.poll() is False
    assert public_projection(model) == expected
    assert model.poll() is False
    assert public_projection(model) == expected


@pytest.mark.parametrize(
    "non_finite",
    [
        pytest.param(float("nan"), id="nan"),
        pytest.param(float("inf"), id="positive-infinity"),
        pytest.param(float("-inf"), id="negative-infinity"),
    ],
)
def test_pi_appended_non_finite_value_keeps_projection(tmp_path, non_finite):
    path = session(tmp_path, rewrite_entries("old value"))
    model = PiModel(path)
    while model.poll():
        pass
    expected = public_projection(model)
    invalid = rewrite_entries("partial new")
    invalid[0]["future"] = {"scores": [non_finite]}

    with open(path, "a", encoding="utf-8") as stream:
        for entry in invalid:
            stream.write(json.dumps(entry) + "\n")

    assert model.poll() is False
    assert public_projection(model) == expected


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("id", ["not", "a", "string"]),
        ("parentSession", 7),
        ("cwd", {"not": "a string"}),
    ],
)
def test_pi_appended_non_string_header_identity_is_atomic(
        tmp_path, field, invalid):
    path = session(tmp_path, rewrite_entries("old value"))
    model = PiModel(path)
    while model.poll():
        pass
    expected = public_projection(model)
    header = pi_fixture.header()
    header[field] = invalid

    with open(path, "a", encoding="utf-8") as stream:
        stream.write(json.dumps(header) + "\n")

    assert model.poll() is False
    assert public_projection(model) == expected


@pytest.mark.parametrize("field", ["id", "parentSession", "cwd"])
def test_pi_runtime_accepts_identity_at_65536_decoded_characters(tmp_path, field):
    header = pi_fixture.header()
    header[field] = "x" * 65_536
    path = session(tmp_path, [header])

    model = PiModel(path)

    assert model.poll() is True
    assert model.poll() is False


@pytest.mark.parametrize("field", ["id", "parentSession", "cwd"])
def test_pi_runtime_rejects_identity_above_65536_decoded_characters(tmp_path, field):
    header = pi_fixture.header()
    header[field] = "x" * 65_537
    path = session(tmp_path, [header])

    model = PiModel(path)
    assert model.poll() is False
    assert model.nodes["main"].steps == []


def test_pi_session_renders_actions(tmp_path):
    model = PiModel(session(tmp_path))
    assert model.poll() is True
    main = model.nodes["main"]
    kinds = [step.kind for step in main.steps]
    assert kinds == ["prompt", "thinking", "text", "tool", "text"]
    assert main.steps[3].title == "bash: uv run pytest -q"
    assert main.model == "claude-sonnet-4-5"
    assert main.tokens == 240
    text, _, order, _ = render_tree(model, "main")
    assert order == ["main"]
    assert "pi" in text.plain


def test_pi_tool_result_pairs_back(tmp_path):
    model = PiModel(session(tmp_path))
    model.poll()
    call = model.nodes["main"].steps[3]
    assert call.status == "done"
    assert call.output == "1 failed, 197 passed"


def test_pi_tool_error_marks_step(tmp_path):
    lines = pi_fixture.entries()
    lines[3]["message"] = pi_fixture.tool_result("call_1", "bash", "boom", is_error=True)
    model = PiModel(session(tmp_path, lines))
    model.poll()
    assert model.nodes["main"].steps[3].status == "error"


def test_pi_turn_state_follows_stop_reason(tmp_path):
    lines = pi_fixture.entries()[:3]
    path = session(tmp_path, lines)
    model = PiModel(path)
    model.poll()
    assert model.nodes["main"].status == "running"    # stopReason toolUse
    pi_fixture.write_session(path, pi_fixture.entries())
    model.poll()
    assert model.nodes["main"].status == "waiting"    # final stopReason stop


def test_pi_aborted_turn_is_error(tmp_path):
    lines = pi_fixture.entries()
    lines[-1]["message"] = pi_fixture.assistant([], stop_reason="aborted")
    lines[-1]["message"]["errorMessage"] = "user cancelled"
    model = PiModel(session(tmp_path, lines))
    model.poll()
    assert model.nodes["main"].status == "error"
    assert model.nodes["main"].result == "user cancelled"


def test_pi_tails_appended_entries(tmp_path):
    path = session(tmp_path, pi_fixture.entries()[:2])
    model = PiModel(path)
    model.poll()
    assert len(model.nodes["main"].steps) == 1
    pi_fixture.write_session(path, pi_fixture.entries())
    assert model.poll() is True
    assert len(model.nodes["main"].steps) == 5


def test_pi_bash_execution_is_an_action(tmp_path):
    lines = pi_fixture.entries()
    lines.append(pi_fixture.message("e5f6g7h8", "d4e5f6g7", {
        "role": "bashExecution", "command": "git status", "output": "clean",
        "exitCode": 0, "cancelled": False, "truncated": False,
        "timestamp": 1_784_000_003_000}))
    model = PiModel(session(tmp_path, lines))
    model.poll()
    step = model.nodes["main"].steps[-1]
    assert (step.kind, step.title, step.output) == ("tool", "bash: git status", "clean")


def test_pi_subagent_result_becomes_a_child_node(tmp_path):
    model = PiModel(session(tmp_path, pi_fixture.subagent_entries()))
    model.poll()
    assert model.nodes["main"].children == ["pi:1"]
    child = model.nodes["pi:1"]
    assert (child.label, child.status, child.kind) == ("researcher", "done", "agent")
    assert [step.kind for step in child.steps] == ["prompt", "text"]
    assert model.nodes["main"].steps[0].kind == "prompt"
    spawn = [step for step in model.nodes["main"].steps if step.kind == "spawn"]
    assert spawn and spawn[0].child == "pi:1"


def test_pi_subagent_results_group_children_by_tool_result(tmp_path):
    model = PiModel(session(tmp_path, pi_fixture.subagent_batch_entries()))
    model.poll()

    assert model.nodes["main"].children == ["dispatch:main:call_9", "dispatch:main:call_10"]
    assert model.nodes["dispatch:main:call_9"].children == ["pi:1", "pi:2"]
    assert model.nodes["dispatch:main:call_10"].children == ["pi:3", "pi:4"]
    assert model.nodes["dispatch:main:call_9"].label == ""
    assert model.nodes["dispatch:main:call_10"].label == ""
    assert [model.nodes[node_id].parent for node_id in ("pi:1", "pi:2", "pi:3", "pi:4")] == [
        "dispatch:main:call_9", "dispatch:main:call_9",
        "dispatch:main:call_10", "dispatch:main:call_10",
    ]


def test_pi_batch_labels_use_result_tasks(tmp_path):
    model = PiModel(session(tmp_path, pi_fixture.subagent_batch_entries()))
    model.poll()

    assert [model.nodes[batch_id].label for batch_id in model.nodes["main"].children] == [
        "",
        "",
    ]
    assert [(model.nodes[node_id].detail, model.nodes[node_id].result,
             model.nodes[node_id].status,
             [step.title for step in model.nodes[node_id].steps])
            for node_id in ("pi:1", "pi:2", "pi:3", "pi:4")] == [
        ("Map call sites", "Found three call sites.", "done",
         ["task: Map call sites", "text: Found three call sites."]),
        ("Review edge cases", "Found one edge case.", "error",
         ["task: Review edge cases", "text: Found one edge case."]),
        ("Draft release notes", "Drafted release notes.", "done",
         ["task: Draft release notes", "text: Drafted release notes."]),
        ("Check regression", "Regression passes.", "done",
         ["task: Check regression", "text: Regression passes."]),
    ]


def test_pi_async_status_builds_live_dispatch_and_child_timelines(tmp_path):
    parent, run_dir, children, _status, _lines = async_fixture(
        tmp_path, child_count=2,
    )
    model = PiModel(str(parent))
    poll_all(model)

    dispatch = model.nodes["dispatch:main:call-async"]
    assert dispatch.children == [
        "pi-async:run-async-1:1", "pi-async:run-async-1:2",
    ]
    assert dispatch.status == "running"
    child = model.nodes[dispatch.children[0]]
    assert (child.label, child.detail, child.status, child.model) == (
        "review-1", "reviewer", "running", "gpt-5.6-sol",
    )
    assert [step.kind for step in child.steps] == ["prompt", "thinking", "tool"]
    assert child.tokens == 7
    spawn = model._steps["call-async"]
    assert (spawn.status, spawn.child, spawn.dispatch) == (
        "running", dispatch.children[0], dispatch.id,
    )
    assert str(run_dir) in model.observation_paths()
    assert set(map(str, children)) <= model.observation_paths()
    assert model.accepts_observation(frozenset({str(run_dir / "status.json")}))


@pytest.mark.parametrize(
    ("run_state", "child_state", "expected"),
    [
        ("complete", "completed", "done"),
        ("failed", "failed", "error"),
        ("rejected", "rejected", "error"),
        ("stopped", "stopped", "error"),
        ("paused", "paused", "waiting"),
        ("running", "paused", "waiting"),
        ("running", "pending", "waiting"),
    ],
)
def test_pi_async_status_maps_canonical_states(
        tmp_path, run_state, child_state, expected):
    parent, _run_dir, _children, _status, _lines = async_fixture(
        tmp_path, state=run_state, child_status=child_state,
    )
    model = PiModel(str(parent))
    poll_all(model)

    child = model.nodes["pi-async:run-async-1:1"]
    assert child.status == expected
    terminal = run_state in {"complete", "failed", "paused", "rejected", "stopped"}
    assert model._async_runs["run-async-1"]["terminal"] is terminal
    if terminal:
        assert model._steps["call-async"].status == expected


def test_pi_async_child_timeline_appends_without_duplicates(tmp_path):
    parent, _run_dir, children, _status, _lines = async_fixture(tmp_path)
    model = PiModel(str(parent))
    poll_all(model)
    child = model.nodes["pi-async:run-async-1:1"]
    assert len(child.steps) == 3

    with open(children[0], "a", encoding="utf-8") as stream:
        stream.write(json.dumps(pi_fixture.message(
            "tool-result", "reply-0",
            pi_fixture.tool_result("read-0", "read", "README contents"),
        )) + "\n")
        stream.write(json.dumps(pi_fixture.message(
            "final", "tool-result",
            pi_fixture.assistant([{"type": "text", "text": "Review complete."}], output=5),
        )) + "\n")
    poll_all(model)

    assert len(child.steps) == 4
    assert child.steps[2].output == "README contents"
    assert child.steps[-1].body == "Review complete."
    assert child.tokens == 12
    poll_all(model)
    assert len(child.steps) == 4


def test_pi_async_real_status_schema_admits_child_without_run_id(tmp_path):
    parent, run_dir, children, status, _lines = async_fixture(
        tmp_path, include_child_run_id=False, agent="worker",
    )
    status["steps"][0]["label"] = "abcd-baseline"
    status["steps"][0]["workflowKey"] = "abcd-baseline"
    (run_dir / "status.json").write_text(json.dumps(status), encoding="utf-8")
    with open(children[0], "a", encoding="utf-8") as stream:
        stream.write(json.dumps(pi_fixture.message(
            "tool-result", "reply-0",
            pi_fixture.tool_result("read-0", "read", "README contents"),
        )) + "\n")
        stream.write(json.dumps(pi_fixture.message(
            "final", "tool-result",
            pi_fixture.assistant(
                [{"type": "text", "text": "Review complete."}],
                model="gpt-5.6-sol", output=5,
            ),
        )) + "\n")

    model = PiModel(str(parent))
    poll_all(model)

    child = model.nodes["pi-async:run-async-1:1"]
    assert (child.label, child.detail, child.model, child.tokens) == (
        "abcd-baseline", "worker", "gpt-5.6-sol", 12,
    )
    assert [step.kind for step in child.steps] == [
        "prompt", "thinking", "tool", "text",
    ]
    assert child.steps[2].output == "README contents"
    assert child.steps[-1].body == "Review complete."
    assert model._async_sessions[str(children[0])] == child.id


def test_pi_async_terminal_projection_survives_artifact_cleanup(tmp_path):
    parent, run_dir, _children, status, _lines = async_fixture(
        tmp_path, state="complete", child_status="completed",
    )
    model = PiModel(str(parent))
    poll_all(model)
    expected = public_projection(model)

    (run_dir / "status.json").unlink()
    (run_dir / "events.jsonl").unlink()
    run_dir.rmdir()
    poll_all(model)

    assert public_projection(model) == expected
    assert model.nodes["pi-async:run-async-1:1"].status == "done"


def test_pi_async_missing_active_artifacts_become_error(tmp_path):
    parent, run_dir, _children, _status, _lines = async_fixture(tmp_path)
    model = PiModel(str(parent))
    poll_all(model)

    (run_dir / "status.json").unlink()
    (run_dir / "events.jsonl").unlink()
    run_dir.rmdir()
    poll_all(model)

    assert model.nodes["pi-async:run-async-1:1"].status == "error"
    assert model._steps["call-async"].status == "error"


@pytest.mark.parametrize(
    ("state", "success", "expected"),
    [("complete", True, "done"), ("paused", False, "waiting")],
)
def test_pi_subagent_wait_completion_is_terminal_fallback(
        tmp_path, state, success, expected):
    parent, run_dir, _children, _status, lines = async_fixture(tmp_path)
    (run_dir / "status.json").unlink()
    (run_dir / "events.jsonl").unlink()
    model = PiModel(str(parent))
    poll_all(model)
    assert model._steps["call-async"].status == "running"

    lines.extend([
        pi_fixture.message("wait-call", "result", pi_fixture.assistant([
            pi_fixture.tool_call("call-wait", "subagent_wait", {"id": "run-async-1"}),
        ], stop_reason="toolUse")),
        pi_fixture.message("wait-result", "wait-call", pi_fixture.tool_result(
            "call-wait", "subagent_wait", "done", details={"completions": [{
                "runId": "run-async-1",
                "state": state,
                "success": success,
                "results": [{
                    "agent": "reviewer", "runId": "child-1", "success": success,
                }],
            }]},
        )),
    ])
    pi_fixture.write_session(parent, lines)
    poll_all(model)

    assert model.nodes["pi-async:run-async-1:1"].status == expected
    assert model._steps["call-async"].status == expected
    assert model._async_runs["run-async-1"]["terminal"] is True


def test_pi_async_rejects_wrong_parent_and_foreign_child_paths(tmp_path):
    parent, run_dir, _children, status, _lines = async_fixture(
        tmp_path, session_id="another-session",
    )
    model = PiModel(str(parent))
    poll_all(model)
    assert "pi-async:run-async-1:1" not in model.nodes
    assert model._steps["call-async"].status == "running"

    foreign = tmp_path.parent / "run-async-1" / "child-1" / "session.jsonl"
    foreign.parent.mkdir(parents=True, exist_ok=True)
    pi_fixture.write_session(foreign, [pi_fixture.header(cwd=str(tmp_path))])
    status["sessionId"] = str(parent)
    status["steps"][0]["sessionFile"] = str(foreign)
    (run_dir / "status.json").write_text(json.dumps(status), encoding="utf-8")
    model = PiModel(str(parent))
    poll_all(model)
    assert str(foreign) not in model._async_sessions


def test_pi_async_rejects_explicit_child_run_mismatch(tmp_path):
    parent, run_dir, children, status, _lines = async_fixture(tmp_path)
    status["steps"][0]["runId"] = "different-child"
    (run_dir / "status.json").write_text(json.dumps(status), encoding="utf-8")

    model = PiModel(str(parent))
    poll_all(model)

    assert str(children[0]) not in model._async_sessions
    assert model.nodes["pi-async:run-async-1:1"].steps == []


def test_pi_async_rejects_symlink_and_non_pi_child_sessions(tmp_path):
    parent, run_dir, children, status, _lines = async_fixture(
        tmp_path, include_child_run_id=False,
    )
    foreign = tmp_path / "foreign.jsonl"
    pi_fixture.write_session(foreign, [pi_fixture.header(cwd=str(tmp_path))])
    linked = parent.with_suffix("") / "linked-child" / "run-0" / "session.jsonl"
    linked.parent.mkdir(parents=True)
    linked.symlink_to(foreign)
    status["steps"][0]["sessionFile"] = str(linked)
    (run_dir / "status.json").write_text(json.dumps(status), encoding="utf-8")

    model = PiModel(str(parent))
    poll_all(model)
    assert str(linked) not in model._async_sessions

    linked.unlink()
    linked.write_text('{"type":"not-pi"}\n', encoding="utf-8")
    poll_all(model)
    assert str(linked) not in model._async_sessions
    assert str(children[0]) not in model._async_sessions


def test_pi_async_snapshot_restores_registry_and_dedupes_child_replay(tmp_path):
    parent, run_dir, children, status, _lines = async_fixture(
        tmp_path, include_child_run_id=False,
    )
    model = PiModel(str(parent))
    poll_all(model)
    snapshot = PiSnapshotCodec().capture(model)
    restored = PiSnapshotCodec().restore(str(parent), snapshot)

    status["state"] = "complete"
    status["steps"][0]["status"] = "completed"
    (run_dir / "status.json").write_text(json.dumps(status), encoding="utf-8")
    with open(children[0], "a", encoding="utf-8") as stream:
        stream.write(json.dumps(pi_fixture.message(
            "final", "reply-0",
            pi_fixture.assistant([{"type": "text", "text": "Finished after restore."}]),
        )) + "\n")
    poll_all(restored)

    child = restored.nodes["pi-async:run-async-1:1"]
    assert child.status == "done"
    assert [step.body for step in child.steps].count("Task 1") == 1
    assert child.steps[-1].body == "Finished after restore."


def foreground_session_fixture(tmp_path):
    parent, _run_dir, children, _status, lines = async_fixture(tmp_path)
    lines[-1]["message"]["details"] = {
        "mode": "single",
        "results": [{
            "agent": "researcher", "task": "Task 1", "exitCode": 0,
            "sessionFile": str(children[0]), "finalOutput": "Saved child output",
            "usage": {"output": 7}, "toolCalls": [{"tool": "read"}],
        }],
    }
    pi_fixture.write_session(parent, lines)
    return parent, children[0], lines


def test_pi_current_foreground_reads_saved_child_and_restores_without_duplicates(tmp_path):
    parent, child_path, _lines = foreground_session_fixture(tmp_path)
    model = PiModel(str(parent))
    poll_all(model)
    child = model.nodes["pi:1"]
    assert child.status == "done"
    assert [step.kind for step in child.steps] == ["prompt", "thinking", "tool"]
    assert child.steps[0].body == "Task 1"
    assert child.steps[-1].title == "read: README.md"
    assert child.tokens == 7
    assert str(child_path) in model.observation_paths()

    snapshot = PiSnapshotCodec().capture(model)
    restored = PiSnapshotCodec().restore(str(parent), snapshot)
    poll_all(restored)
    assert restored.nodes["pi:1"].steps == child.steps
    assert restored.nodes["pi:1"].tokens == 7


def test_pi_current_foreground_missing_session_keeps_recorded_result(tmp_path):
    parent, child_path, _lines = foreground_session_fixture(tmp_path)
    child_path.unlink()
    model = PiModel(str(parent))
    poll_all(model)
    child = model.nodes["pi:1"]
    assert [step.kind for step in child.steps] == ["prompt", "text"]
    assert child.result == "Saved child output"
    assert child.tokens == 7
    assert model._async_sessions == {}


@pytest.mark.parametrize(("field", "value"), [
    ("sessionFile", []), ("finalOutput", {}), ("usage", {"output": True}),
])
def test_pi_current_foreground_typed_result_fields_reject_append_atomically(
        tmp_path, field, value):
    parent, _child_path, lines = foreground_session_fixture(tmp_path)
    result = lines.pop()
    pi_fixture.write_session(parent, lines)
    model = PiModel(str(parent))
    poll_all(model)
    before = public_projection(model)
    result["message"]["details"]["results"][0][field] = value
    with parent.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(result) + "\n")
    assert model.poll() is False
    assert public_projection(model) == before


@pytest.mark.parametrize("unsafe", ["foreign", "symlink"])
def test_pi_current_foreground_refuses_foreign_or_linked_transcripts(tmp_path, unsafe):
    parent, child_path, lines = foreground_session_fixture(tmp_path)
    foreign = tmp_path / "foreign.jsonl"
    pi_fixture.write_session(foreign, [
        pi_fixture.header(),
        pi_fixture.message("private", None, pi_fixture.user("Foreign private content")),
    ])
    if unsafe == "foreign":
        lines[-1]["message"]["details"]["results"][0]["sessionFile"] = str(foreign)
        pi_fixture.write_session(parent, lines)
    else:
        child_path.unlink()
        child_path.symlink_to(foreign)
    model = PiModel(str(parent))
    poll_all(model)
    assert model._async_sessions == {}
    assert all("Foreign private content" not in step.body
               for step in model.nodes["pi:1"].steps)


def test_pi_child_sessions_accept_aliases_above_selected_session_tree(tmp_path):
    parent, _run_dir, children, status, _lines = async_fixture(tmp_path)
    alias = tmp_path.parent / (tmp_path.name + "-alias")
    alias.symlink_to(tmp_path, target_is_directory=True)
    try:
        status["steps"][0]["sessionFile"] = str(alias / children[0].relative_to(tmp_path))
        (_run_dir / "status.json").write_text(json.dumps(status), encoding="utf-8")
        model = PiModel(str(parent))
        poll_all(model)
        assert str(children[0]) in model._async_sessions
        assert len(model.nodes["pi-async:run-async-1:1"].steps) == 3
        restored = PiSnapshotCodec().restore(str(parent), PiSnapshotCodec().capture(model))
        poll_all(restored)
        assert len(restored.nodes["pi-async:run-async-1:1"].steps) == 3
    finally:
        alias.unlink()


@pytest.mark.parametrize("invalid", [
    None, "runId", "sessionId", "sessionId-type", "parentWorkflowRunId", "sessionFile",
])
def test_pi_current_workflow_child_identity_requires_correlated_status(tmp_path, invalid):
    parent, run_dir, children, status, _lines = async_fixture(tmp_path)
    child_run_id = "detached-child-run"
    status["steps"][0]["runId"] = child_run_id
    child_status = {
        "runId": child_run_id, "sessionId": str(parent),
        "parentWorkflowRunId": status["runId"], "sessionFile": str(children[0]),
    }
    if invalid == "sessionId-type":
        child_status["sessionId"] = 7
    elif invalid:
        child_status[invalid] = "wrong-identity"
    child_run_dir = run_dir.parent / child_run_id
    child_run_dir.mkdir()
    (child_run_dir / "status.json").write_text(json.dumps(child_status), encoding="utf-8")
    (run_dir / "status.json").write_text(json.dumps(status), encoding="utf-8")

    model = PiModel(str(parent))
    poll_all(model)
    child = model.nodes["pi-async:run-async-1:1"]
    if invalid:
        assert model._async_sessions == {}
        assert child.steps == []
    else:
        assert str(children[0]) in model._async_sessions
        assert [step.kind for step in child.steps] == ["prompt", "thinking", "tool"]
        assert child.tokens == 7


def test_pi_current_workflow_running_child_uses_step_session_identity(tmp_path):
    parent, run_dir, children, status, _lines = async_fixture(tmp_path)
    child_run_id = "detached-child-run"
    status["steps"][0]["runId"] = child_run_id
    child_run_dir = run_dir.parent / child_run_id
    (run_dir / "status.json").write_text(json.dumps(status), encoding="utf-8")
    model = PiModel(str(parent))
    poll_all(model)
    assert model.nodes["pi-async:run-async-1:1"].steps == []
    assert str(child_run_dir) in model.observation_paths()
    child_status_path = child_run_dir / "status.json"
    assert model.accepts_observation({str(child_status_path)})
    restored = PiSnapshotCodec().restore(str(parent), PiSnapshotCodec().capture(model))
    assert str(child_run_dir) in restored.observation_paths()

    child_run_dir.mkdir()
    child_status = {
        "runId": child_run_id, "sessionId": str(parent),
        "parentWorkflowRunId": status["runId"], "state": "running",
        "steps": [{"agent": "reviewer", "sessionFile": str(children[0])}],
    }
    child_status_path.write_text(json.dumps(child_status), encoding="utf-8")
    poll_all(model)
    poll_all(restored)
    child = model.nodes["pi-async:run-async-1:1"]
    assert child.status == "running"
    assert child.steps[-1].title == "read: README.md"
    assert child.steps[-1].status == "running"
    assert restored.nodes[child.id].steps == child.steps


def test_pi_session_directory_encodes_working_directory(monkeypatch):
    monkeypatch.delenv("PI_CODING_AGENT_SESSION_DIR", raising=False)
    monkeypatch.setenv("PI_CODING_AGENT_DIR", "/agent")
    assert pi_session_slug("/work/project") == "--work-project--"
    assert pi_session_dir("/work/project") == "/agent/sessions/--work-project--"


def test_pi_session_dir_override_replaces_location(monkeypatch, tmp_path):
    monkeypatch.setenv("PI_CODING_AGENT_SESSION_DIR", str(tmp_path))
    assert pi_session_dir("/work/project") == str(tmp_path)


def test_newest_pi_session_ignores_non_sessions(monkeypatch, tmp_path):
    monkeypatch.setenv("PI_CODING_AGENT_SESSION_DIR", str(tmp_path))
    (tmp_path / "junk.jsonl").write_text("{}\n", encoding="utf-8")
    assert newest_pi_session("/work/project") is None
    path = session(tmp_path)
    assert newest_pi_session("/work/project") == path


def test_pi_deep_first_record_is_malformed_and_skipped_by_newest_session(
        monkeypatch, tmp_path):
    monkeypatch.setenv("PI_CODING_AGENT_SESSION_DIR", str(tmp_path))
    valid = session(tmp_path, name="100-valid.jsonl")
    malformed = tmp_path / "200-deep.jsonl"
    malformed.write_text(
        "[" * 10_000 + "0" + "]" * 10_000 + "\n",
        encoding="utf-8",
    )
    os.utime(valid, (100, 100))
    os.utime(malformed, (200, 200))

    invalid = PiModel(str(malformed))
    assert invalid.poll() is False
    assert invalid.nodes["main"].steps == []
    assert newest_pi_session(pi_fixture.CWD) == valid


def test_pi_provider_is_detected_and_opened(tmp_path):
    path = session(tmp_path)
    assert detect_provider(path) == "pi"
    assert open_model(path, "pi").provider == "pi"
    with pytest.raises(ValueError):
        open_model(path, "codex")


def test_non_pi_file_is_refused(tmp_path):
    path = tmp_path / "other.jsonl"
    path.write_text('{"type":"session_meta","payload":{"id":"x"}}\n', encoding="utf-8")
    model = PiModel(str(path))
    assert model.poll() is False
    assert model.nodes["main"].steps == []


def test_pi_skill_read_names_skill(tmp_path):
    lines = pi_fixture.entries()
    lines[2]["message"] = pi_fixture.assistant([
        pi_fixture.tool_call("call_2", "read", {"path": "/x/skills/refine/SKILL.md"}),
    ], stop_reason="toolUse")
    model = PiModel(session(tmp_path, lines))
    model.poll()
    assert model.nodes["main"].skills == ["refine"]


@pytest.mark.parametrize(
    ("depth", "accepted"),
    [
        pytest.param(128, True, id="depth-128"),
        pytest.param(129, False, id="depth-129"),
    ],
)
def test_pi_runtime_uses_shared_nesting_boundary(tmp_path, depth, accepted):
    path = session(tmp_path, rewrite_entries("old value"))
    model = PiModel(path)
    while model.poll():
        pass
    expected = public_projection(model)
    addition = rewrite_entries("boundary value")[1]
    addition["future"] = nested_value(depth - 1)
    with open(path, "a", encoding="utf-8") as stream:
        stream.write(json.dumps(addition) + "\n")

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
def test_pi_known_usage_output_uses_exact_digit_boundary(
    tmp_path, digits, accepted,
):
    path = session(tmp_path, rewrite_entries("old value"))
    model = PiModel(path)
    while model.poll():
        pass
    expected = public_projection(model)
    message = pi_fixture.assistant([
        {"type": "text", "text": "must stay hidden"},
    ])
    message["usage"]["output"] = "LONG_INTEGER"
    addition = pi_fixture.message("long-usage", None, message)
    encoded = json.dumps(addition).encode("utf-8").replace(
        b'"LONG_INTEGER"', b"9" * digits,
    )
    with open(path, "ab") as stream:
        stream.write(encoded + b"\n")

    assert model.poll() is accepted
    if accepted:
        assert model.nodes["main"].tokens == 120 + int("9" * digits)
    else:
        assert public_projection(model) == expected
        assert model.nodes["main"].tokens == 120
    assert model.poll() is False


@pytest.mark.parametrize("case", ["bash", "child"])
@pytest.mark.parametrize(
    ("digits", "accepted"),
    [
        pytest.param(128, True, id="128-digits"),
        pytest.param(129, False, id="129-digits"),
    ],
)
def test_pi_known_exit_code_uses_exact_digit_boundary(
    tmp_path, case, digits, accepted,
):
    if case == "bash":
        initial = rewrite_entries("old value")
        addition = pi_fixture.message("long-exit", None, {
            "role": "bashExecution",
            "command": "false",
            "output": "failed",
            "exitCode": "LONG_INTEGER",
            "cancelled": False,
        })
    else:
        entries = pi_fixture.subagent_entries()
        initial = entries[:-1]
        addition = entries[-1]
        addition["message"]["details"]["results"][0]["exitCode"] = "LONG_INTEGER"
    path = session(tmp_path, initial)
    model = PiModel(path)
    while model.poll():
        pass
    expected = public_projection(model)
    encoded = json.dumps(addition).encode("utf-8").replace(
        b'"LONG_INTEGER"', b"9" * digits,
    )
    with open(path, "ab") as stream:
        stream.write(encoded + b"\n")

    assert model.poll() is accepted
    if not accepted:
        assert public_projection(model) == expected
    assert model.poll() is False


@pytest.mark.parametrize(
    "token",
    [
        pytest.param(b"1" + b"0" * 128, id="129-digit-integer"),
        pytest.param(b"1e999", id="large-finite-exponent"),
    ],
)
def test_pi_unknown_numeric_token_does_not_reject_visible_record(tmp_path, token):
    path = session(tmp_path, rewrite_entries("old value"))
    model = PiModel(path)
    while model.poll():
        pass
    addition = json.dumps(rewrite_entries("accepted value")[1]).encode("utf-8")
    addition = addition[:-1] + b',"future":' + token + b"}\n"
    with open(path, "ab") as stream:
        stream.write(addition)

    assert model.poll() is True
    assert [step.body for step in model.nodes["main"].steps] == [
        "old value", "accepted value",
    ]
    assert model.poll() is False


def test_pi_bare_skill_file_names_skill():
    assert skill_mode.skill_of("read", {"path": "~/.pi/agent/skills/tdd.md"}) == "tdd"
    assert skill_mode.skill_of("read", {"path": "/x/notes/README.md"}) == ""


def test_pi_workflow_signals(tmp_path):
    lines = pi_fixture.entries()
    lines[2]["message"] = pi_fixture.assistant([
        pi_fixture.tool_call("call_2", "read",
                             {"path": "/s/skills/to-tickets/SKILL.md"}),
        pi_fixture.tool_call("call_3", "write",
                             {"path": ".scratch/stack/issues/02-amend.md",
                              "content": "**Status:** ready-for-agent\n"}),
        pi_fixture.tool_call("call_4", "edit",
                             {"path": ".scratch/stack/issues/02-amend.md",
                              "oldText": "**Status:** ready-for-agent",
                              "newText": "**Status:** done"}),
        pi_fixture.tool_call("call_5", "bash",
                             {"command": "blackboard read --project . --board b1"}),
        pi_fixture.tool_call("call_6", "subagent",
                             {"agent": "reviewer",
                              "task": "review-falsification on the diff"}),
    ], stop_reason="toolUse")
    model = PiModel(session(tmp_path, lines))
    model.poll()
    main = model.nodes["main"]
    assert main.skills == ["to-tickets"]
    assert main.workflow == ["ticket 02-amend created", "ticket 02-amend → done",
                            "board read", "review dispatched"]
    assert main.phase == "review"
