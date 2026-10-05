"""=== SCHEMA-DERIVED PI FIXTURE ===

Every entry below was written from pi's own shipped source of truth, not copied
from a running agent:

  @mariozechner/pi-coding-agent 0.73.1
    docs/session-format.md                 entry types, roles, content blocks
    dist/core/session-manager.js           header, id/parentId, append order
    dist/core/tools/{bash,edit,write}.js   tool argument names
    examples/extensions/subagent/index.ts  subagent tool result `details` shape

Core messages and built-in tool calls were replayed through `PiModel` from a live
`@earendil-works/pi-coding-agent` 0.83.0 session. Subagent entries remain
schema-derived: that tool is an example extension, so its `details` shape is
extension-defined rather than part of pi's core schema.
"""
import json

CWD = "/work/project"
SESSION_ID = "0193c0de-0000-7000-8000-000000000001"


def header(cwd=CWD, session_id=SESSION_ID):
    return {"type": "session", "version": 3, "id": session_id,
            "timestamp": "2026-07-28T10:00:00.000Z", "cwd": cwd}


def message(entry_id, parent_id, message, ts="2026-07-28T10:00:01Z"):
    return {"type": "message", "id": entry_id, "parentId": parent_id,
            "timestamp": ts, "message": message}


def user(text):
    return {"role": "user", "content": text, "timestamp": 1_784_000_000_000}


def assistant(content, stop_reason="stop", model="claude-sonnet-4-5", output=120):
    return {"role": "assistant", "content": content, "api": "anthropic-messages",
            "provider": "anthropic", "model": model, "stopReason": stop_reason,
            "usage": {"input": 900, "output": output, "cacheRead": 0, "cacheWrite": 0,
                      "totalTokens": 900 + output,
                      "cost": {"input": 0.0, "output": 0.0, "cacheRead": 0.0,
                               "cacheWrite": 0.0, "total": 0.0}},
            "timestamp": 1_784_000_001_000}


def tool_call(call_id, name, arguments):
    return {"type": "toolCall", "id": call_id, "name": name, "arguments": arguments}


def tool_result(call_id, name, text, is_error=False, details=None):
    result = {"role": "toolResult", "toolCallId": call_id, "toolName": name,
              "content": [{"type": "text", "text": text}], "isError": is_error,
              "timestamp": 1_784_000_002_000}
    if details is not None:
        result["details"] = details
    return result


def entries():
    """A short session: a prompt, a thought, a bash call, and its result."""
    return [
        header(),
        message("a1b2c3d4", None, user("Summarise the failing test")),
        message("b2c3d4e5", "a1b2c3d4", assistant([
            {"type": "thinking", "thinking": "Read the suite first."},
            {"type": "text", "text": "Running the suite."},
            tool_call("call_1", "bash", {"command": "uv run pytest -q"}),
        ], stop_reason="toolUse")),
        message("c3d4e5f6", "b2c3d4e5",
                tool_result("call_1", "bash", "1 failed, 197 passed")),
        message("d4e5f6g7", "c3d4e5f6",
                assistant([{"type": "text", "text": "One assertion is stale."}])),
    ]


def subagent_entries():
    """A `subagent` tool call whose result embeds the child's whole transcript."""
    child = [
        assistant([{"type": "text", "text": "Found three call sites."}]),
    ]
    details = {"mode": "single", "agentScope": "user", "results": [{
        "agent": "researcher", "agentSource": "user", "task": "Map the call sites",
        "exitCode": 0, "messages": child, "stderr": "",
        "usage": {"input": 200, "output": 40}, "model": "gpt-5",
        "stopReason": "stop",
    }]}
    return [
        header(),
        message("a1b2c3d4", None, user("Map the call sites")),
        message("b2c3d4e5", "a1b2c3d4", assistant([
            tool_call("call_9", "subagent",
                      {"agent": "researcher", "task": "Map the call sites"}),
        ], stop_reason="toolUse")),
        message("c3d4e5f6", "b2c3d4e5",
                tool_result("call_9", "subagent", "Found three call sites.",
                            details=details)),
    ]


def subagent_batch_entries():
    """Two subagent results, each carrying two complete child transcripts."""
    def result(agent, task, text, exit_code=0):
        return {
            "agent": agent, "agentSource": "user", "task": task,
            "exitCode": exit_code,
            "messages": [assistant([{"type": "text", "text": text}])],
            "stderr": "", "usage": {"input": 200, "output": 40},
            "model": "gpt-5", "stopReason": "stop",
        }

    first = {"mode": "parallel", "agentScope": "user", "results": [
        result("researcher", "Map call sites", "Found three call sites."),
        result("reviewer", "Review edge cases", "Found one edge case.", 1),
    ]}
    second = {"mode": "parallel", "agentScope": "user", "results": [
        result("writer", "Draft release notes", "Drafted release notes."),
        result("tester", "Check regression", "Regression passes."),
    ]}
    return [
        header(),
        message("a1b2c3d4", None, user("Prepare release")),
        message("b2c3d4e5", "a1b2c3d4", assistant([
            tool_call("call_9", "subagent", {"agent": "researcher", "task": "Map call sites"}),
        ], stop_reason="toolUse")),
        message("c3d4e5f6", "b2c3d4e5", tool_result(
            "call_9", "subagent", "First group complete.", details=first)),
        message("d4e5f6g7", "c3d4e5f6", assistant([
            tool_call("call_10", "subagent", {"agent": "writer", "task": "Draft release notes"}),
        ], stop_reason="toolUse")),
        message("e5f6g7h8", "d4e5f6g7", tool_result(
            "call_10", "subagent", "Second group complete.", details=second)),
    ]


def write_session(path, lines):
    with open(path, "w", encoding="utf-8") as stream:
        for line in lines:
            stream.write(json.dumps(line) + "\n")
    return str(path)
