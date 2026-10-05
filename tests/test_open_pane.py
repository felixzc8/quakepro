import json
import os
from pathlib import Path
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
from typing import Optional
import fcntl

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "codex-open-pane.sh"


FAKE_TMUX = r'''#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys
import time

state_path = Path(os.environ["FAKE_TMUX_STATE"])
log_path = Path(os.environ["FAKE_TMUX_LOG"])
args = sys.argv[1:]
with log_path.open("a", encoding="utf-8") as stream:
    stream.write(json.dumps(args) + "\n")
command_args = args[2:] if args[:1] == ["-S"] else args
command = command_args[0] if command_args else ""
try:
    state = json.loads(state_path.read_text(encoding="utf-8"))
except (FileNotFoundError, json.JSONDecodeError):
    state = {"titles": [], "splits": 0}

def save():
    temporary = state_path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state), encoding="utf-8")
    os.replace(temporary, state_path)

failure = os.environ.get("FAKE_TMUX_FAIL", "")
if command == "list-panes":
    time.sleep(float(os.environ.get("FAKE_LIST_DELAY", "0")))
    if failure == command:
        raise SystemExit(1)
    if "#{pane_id}" in command_args[-1]:
        print("\n".join(f"%{index + 2} {title}" for index, title in enumerate(state["titles"])))
    else:
        print("\n".join(state["titles"]))
elif command == "display-message":
    print(os.environ.get("FAKE_ACTIVE_PANE", "%1"))
elif command == "split-window":
    if failure == command:
        raise SystemExit(1)
    state["splits"] += 1
    save()
    time.sleep(float(os.environ.get("FAKE_SPLIT_DELAY", "0")))
    print(f"%{state['splits'] + 1}")
elif command == "select-pane" and "-T" in command_args:
    time.sleep(float(os.environ.get("FAKE_TITLE_DELAY", "0")))
    if failure == "select-title":
        raise SystemExit(1)
    title = command_args[command_args.index("-T") + 1]
    if title not in state["titles"]:
        state["titles"].append(title)
        save()
raise SystemExit(0)
'''


FAKE_UNAME = r'''#!/bin/sh
printf '%s\n' "${FAKE_UNAME:-Linux}"
'''


FAKE_OSASCRIPT = r'''#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys
import time

with Path(os.environ["FAKE_OSASCRIPT_LOG"]).open("a", encoding="utf-8") as stream:
    stream.write(json.dumps(sys.argv[1:]) + "\n")
if any('exists application "iTerm2"' in value for value in sys.argv[1:]):
    print(os.environ.get("FAKE_ITERM_EXISTS", "false"))
    raise SystemExit(0)
time.sleep(float(os.environ.get("FAKE_OSASCRIPT_DELAY", "0")))
raise SystemExit(int(os.environ.get("FAKE_OSASCRIPT_STATUS", "0")))
'''


FAKE_PS = r'''#!/usr/bin/env python3
import json
import os
import sys
import time

time.sleep(float(os.environ.get("FAKE_PS_DELAY", "0")))
if "eww" in sys.argv:
    pid = sys.argv[sys.argv.index("-p") + 1]
    environments = json.loads(os.environ.get("FAKE_PS_ENVIRONMENTS", "{}"))
    inherited = {
        "TMUX_PANE": os.environ.get("TMUX_PANE", ""),
        "TMUX": os.environ.get("TMUX", ""),
        "HERDR_PANE_ID": os.environ.get("HERDR_PANE_ID", ""),
        "HERDR_SOCKET_PATH": os.environ.get("HERDR_SOCKET_PATH", ""),
    }
    selected = environments.get(pid, inherited) if environments else {}
    values = [
        f"TMUX_PANE={selected.get('TMUX_PANE', os.environ.get('FAKE_PS_PANE', '%9'))}",
        f"TMUX={selected.get('TMUX', os.environ.get('FAKE_PS_TMUX', '/tmp/ancestor.sock,44,0'))}",
        f"HERDR_PANE_ID={selected.get('HERDR_PANE_ID', os.environ.get('FAKE_PS_HERDR_PANE', 'w2:p1'))}",
        f"HERDR_SOCKET_PATH={selected.get('HERDR_SOCKET_PATH', os.environ.get('FAKE_PS_HERDR_SOCKET', '/tmp/herdr.sock'))}",
    ]
    print("process " + " ".join(values))
else:
    chain = json.loads(os.environ.get(
        "FAKE_PS_CHAIN",
        '["pane_lifecycle", "codex", "/bin/zsh", "tmux: server"]',
    ))
    pid = os.getppid()
    for index, command in enumerate(chain):
        parent = 1 if index == len(chain) - 1 else 9000 + index
        print(f"{pid if index == 0 else 8999 + index} {parent} {command}")
'''


FAKE_HERDR = r'''#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys
import time

state_path = Path(os.environ["FAKE_HERDR_STATE"])
log_path = Path(os.environ["FAKE_HERDR_LOG"])
args = sys.argv[1:]
with log_path.open("a", encoding="utf-8") as stream:
    stream.write(json.dumps({
        "args": args,
        "socket": os.environ.get("HERDR_SOCKET_PATH"),
    }) + "\n")
state = json.loads(state_path.read_text(encoding="utf-8"))
command = args[:2]
pane_id = ""
if command == ["pane", "current"]:
    pane_id = args[args.index("--pane") + 1]
    pane = state["panes"].get(pane_id)
    if pane is None:
        raise SystemExit(1)
    print(json.dumps({"result": {"pane": pane}}))
elif command == ["pane", "get"]:
    pane = state["panes"].get(args[2])
    if pane is None:
        raise SystemExit(1)
    print(json.dumps({"result": {"pane": pane}}))
elif command == ["pane", "split"]:
    if os.environ.get("FAKE_HERDR_FAIL") == "split":
        raise SystemExit(1)
    time.sleep(float(os.environ.get("FAKE_HERDR_SPLIT_DELAY", "0")))
    state["splits"] += 1
    pane_id = f"w2:p{state['splits'] + 1}"
    pane = {"pane_id": pane_id, "label": "", "command": ""}
    state["panes"][pane_id] = pane
    print(json.dumps({"result": {"pane": pane}}))
elif command == ["pane", "rename"]:
    pane_id = args[2]
    if os.environ.get("FAKE_HERDR_FAIL") == "rename":
        raise SystemExit(1)
    state["panes"][pane_id]["label"] = " ".join(args[3:])
elif command == ["pane", "run"]:
    pane_id = args[2]
    if os.environ.get("FAKE_HERDR_FAIL") == "run":
        raise SystemExit(1)
    state["panes"][pane_id]["command"] = args[3]
elif command == ["pane", "close"]:
    state["panes"].pop(args[2], None)
temporary = state_path.with_suffix(".tmp")
temporary.write_text(json.dumps(state), encoding="utf-8")
os.replace(temporary, state_path)
'''


def executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def assert_app_launch(command: str, bundle: Path, *args: str) -> None:
    words = shlex.split(command)
    index = words.index(str(bundle / "quakepro"))
    assert words[index - 1:] == ["exec", str(bundle / "quakepro"), *args]


@pytest.fixture
def runtime(tmp_path):
    bundle = tmp_path / "Quake Pro's bundle"
    bundle.mkdir()
    executable(bundle / "quakepro", "#!/bin/sh\nexit 0\n")

    binaries = tmp_path / "bin"
    binaries.mkdir()
    executable(binaries / "tmux", FAKE_TMUX)
    executable(binaries / "uname", FAKE_UNAME)
    executable(binaries / "osascript", FAKE_OSASCRIPT)
    executable(binaries / "ps", FAKE_PS)
    executable(binaries / "herdr", FAKE_HERDR)

    state = tmp_path / "tmux-state.json"
    state.write_text(json.dumps({"titles": [], "splits": 0}), encoding="utf-8")
    herdr_state = tmp_path / "herdr-state.json"
    herdr_state.write_text(json.dumps({
        "panes": {"w2:p1": {"pane_id": "w2:p1", "label": "origin", "command": ""}},
        "splits": 0,
    }), encoding="utf-8")
    env = os.environ.copy()
    env.update({
        "PATH": str(binaries) + os.pathsep + env.get("PATH", ""),
        "PYTHONPATH": str(ROOT / "src"),
        "QUAKEPRO_EXECUTABLE": str(bundle / "quakepro"),
        "CODEX_HOME": str(tmp_path / "codex-home"),
        "TMPDIR": str(tmp_path / "tmp"),
        "QUAKEPRO_STATE_DIR": str(tmp_path / "quakepro-state"),
        "TMUX_PANE": "%1",
        "TMUX": "/tmp/fake socket,123,0",
        "FAKE_PS_PANE": "%1",
        "FAKE_PS_TMUX": "/tmp/fake socket,123,0",
        "FAKE_TMUX_STATE": str(state),
        "FAKE_TMUX_LOG": str(tmp_path / "tmux.log"),
        "FAKE_OSASCRIPT_LOG": str(tmp_path / "osascript.log"),
        "FAKE_HERDR_STATE": str(herdr_state),
        "FAKE_HERDR_LOG": str(tmp_path / "herdr.log"),
        "HERDR_PANE_ID": "w2:p1",
        "HERDR_SOCKET_PATH": "/tmp/herdr.sock",
    })
    return bundle, env, state


def rollout(path: Path, thread_id: str, session_id: Optional[str] = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"id": thread_id, "cwd": str(path.parent)}
    if session_id is not None:
        payload["session_id"] = session_id
    path.write_text(json.dumps({"type": "session_meta", "payload": payload}) + "\n",
                    encoding="utf-8")
    return path


def hook_payload(session_id: str, transcript_path):
    return {
        "hook_event_name": "SubagentStart",
        "session_id": session_id,
        "transcript_path": str(transcript_path) if transcript_path is not None else None,
        "agent_id": "child",
    }


def hook_command():
    return [
        sys.executable,
        "-m",
        "quakepro.cli",
        "_hook",
        "codex",
        "--quakepro-hook-id",
        "quakepro.codex-auto-open.v1",
    ]


def run_hook(bundle: Path, env: dict[str, str], payload, timeout: float = 8):
    data = payload if isinstance(payload, str) else json.dumps(payload)
    result = subprocess.run(
        hook_command(),
        input=data,
        text=True,
        capture_output=True,
        env=env,
        timeout=timeout,
    )
    assert result.returncode == 0
    expected = "{}\n" if (
        isinstance(payload, dict)
        and payload.get("hook_event_name") in {"Stop", "SubagentStop"}
        and isinstance(payload.get("session_id"), str)
    ) else ""
    assert result.stdout == expected
    assert result.stderr == ""
    return result


def tmux_log(env):
    path = Path(env["FAKE_TMUX_LOG"])
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def tmux_state(path):
    return json.loads(path.read_text(encoding="utf-8"))


def command_args(entry):
    return entry[2:] if entry[:1] == ["-S"] else entry


def herdr_log(env):
    path = Path(env["FAKE_HERDR_LOG"])
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_codex_shell_adapter_dispatches_provider(tmp_path):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    shutil.copy2(SCRIPT, bundle / SCRIPT.name)
    package = bundle / "src" / "quakepro"
    package.mkdir(parents=True)
    (package / "pane_lifecycle.py").write_text("", encoding="utf-8")
    python = bundle / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    argv_log = tmp_path / "argv.json"
    python.write_text(
        "#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$ARGV_LOG\"\n",
        encoding="utf-8",
    )
    python.chmod(0o755)

    result = subprocess.run(
        [str(bundle / SCRIPT.name), "--quakepro-hook-id", "owner"],
        input="{}",
        text=True,
        capture_output=True,
        env={**os.environ, "ARGV_LOG": str(argv_log)},
        timeout=5,
    )

    assert result.returncode == 0
    assert result.stdout == result.stderr == ""
    assert argv_log.read_text(encoding="utf-8").splitlines() == [
        "-m",
        "quakepro.pane_lifecycle",
        "codex",
        "--quakepro-hook-id",
        "owner",
    ]


def test_tmux_launch_uses_exact_session_socket_title_and_quoted_path(runtime):
    bundle, env, state_path = runtime
    session_id = "019-session"
    transcript = rollout(
        Path(env["CODEX_HOME"]) / "sessions" / "2026" / "07" / "09" / "roll out's file.jsonl",
        session_id,
    )

    run_hook(bundle, env, hook_payload(session_id, transcript))

    log = tmux_log(env)
    list_call = next(entry for entry in log if "list-panes" in entry)
    assert list_call == ["-S", "/tmp/fake socket", "list-panes", "-a", "-F", "#{pane_title}"]
    split = next(command_args(entry) for entry in log if "split-window" in entry)
    assert split[:-1] == [
        "split-window", "-d", "-h", "-l", "40%", "-t", "%1",
        "-P", "-F", "#{pane_id}",
    ]
    title = f"quakepro:codex:{session_id}"
    assert_app_launch(
        split[-1],
        bundle,
        "--provider",
        "codex",
        "--file",
        str(transcript),
    )
    assert 'select-pane -t "$TMUX_PANE"' in split[-1]
    assert f"-T {shlex.quote(title)}" in split[-1]
    assert title in tmux_state(state_path)["titles"]
    calls = [command_args(entry) for entry in log]
    assert ["select-pane", "-t", "%1"] in calls
    assert ["set", "-t", "%1", "mouse", "on"] in calls


def test_existing_server_wide_exact_title_skips_split(runtime):
    bundle, env, state_path = runtime
    session_id = "same-session"
    transcript = rollout(Path(env["CODEX_HOME"]) / "sessions" / "root.jsonl", session_id)
    state_path.write_text(json.dumps({
        "titles": [f"prefix-quakepro:codex:{session_id}", f"quakepro:codex:{session_id}"],
        "splits": 0,
    }), encoding="utf-8")

    run_hook(bundle, env, hook_payload(session_id, transcript))

    assert tmux_state(state_path)["splits"] == 0
    assert not any("split-window" in entry for entry in tmux_log(env))


def test_similar_title_is_not_treated_as_same_session(runtime):
    bundle, env, state_path = runtime
    session_id = "session-a"
    transcript = rollout(Path(env["CODEX_HOME"]) / "sessions" / "root.jsonl", session_id)
    state_path.write_text(json.dumps({
        "titles": [f"quakepro:codex:{session_id}-other"],
        "splits": 0,
    }), encoding="utf-8")

    run_hook(bundle, env, hook_payload(session_id, transcript))

    assert tmux_state(state_path)["splits"] == 1


def test_concurrent_starts_open_one_tmux_pane(runtime):
    bundle, env, state_path = runtime
    session_id = "concurrent-session"
    transcript = rollout(Path(env["CODEX_HOME"]) / "sessions" / "root.jsonl", session_id)
    env["FAKE_SPLIT_DELAY"] = "0.2"
    command = hook_command()
    data = json.dumps(hook_payload(session_id, transcript))

    processes = [subprocess.Popen(
        command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, env=env,
    ) for _ in range(2)]
    outputs = [process.communicate(data, timeout=8) for process in processes]

    assert all(process.returncode == 0 for process in processes)
    assert outputs == [("", ""), ("", "")]
    assert tmux_state(state_path)["splits"] == 1


def test_different_parent_sessions_each_open(runtime):
    bundle, env, state_path = runtime
    for session_id in ("parent-one", "parent-two"):
        transcript = rollout(
            Path(env["CODEX_HOME"]) / "sessions" / f"{session_id}.jsonl", session_id
        )
        run_hook(bundle, env, hook_payload(session_id, transcript))

    state = tmux_state(state_path)
    assert state["splits"] == 2
    assert set(state["titles"]) == {
        "quakepro:codex:parent-one",
        "quakepro:codex:parent-two",
    }


def test_null_transcript_fails_closed_without_scanning_history(runtime):
    bundle, env, state_path = runtime
    sessions = Path(env["CODEX_HOME"]) / "sessions"
    rollout(sessions / "old" / "wanted.jsonl", "wanted-session")
    rollout(sessions / "new" / "wrong.jsonl", "wrong-session")

    run_hook(bundle, env, hook_payload("wanted-session", None))

    assert tmux_state(state_path)["splits"] == 0
    assert not any("split-window" in entry for entry in tmux_log(env))


def test_child_transcript_for_parent_session_is_accepted(runtime):
    bundle, env, _ = runtime
    transcript = rollout(
        Path(env["CODEX_HOME"]) / "sessions" / "child.jsonl",
        "child-thread",
        "parent-thread",
    )

    run_hook(bundle, env, hook_payload("parent-thread", transcript))

    split = next(command_args(entry) for entry in tmux_log(env) if "split-window" in entry)
    assert shlex.split(split[-1])[-1] == str(transcript)


def test_transcript_creation_race_is_retried(runtime):
    bundle, env, state_path = runtime
    transcript = Path(env["CODEX_HOME"]) / "sessions" / "late.jsonl"
    timer = threading.Timer(0.08, rollout, args=(transcript, "late-session"))
    timer.start()
    try:
        run_hook(bundle, env, hook_payload("late-session", transcript))
    finally:
        timer.join()

    assert tmux_state(state_path)["splits"] == 1


@pytest.mark.parametrize("payload", [
    "not json",
    [],
    {"hook_event_name": "Stop", "session_id": "valid"},
    {"hook_event_name": "SubagentStart", "session_id": "../unsafe"},
    {"hook_event_name": "SubagentStart", "session_id": None},
])
def test_invalid_payload_is_silent(runtime, payload):
    bundle, env, state_path = runtime

    run_hook(bundle, env, payload)

    assert tmux_state(state_path)["splits"] == 0


def test_mismatched_transcript_never_attaches_to_it(runtime):
    bundle, env, state_path = runtime
    wrong = rollout(Path(env["CODEX_HOME"]) / "outside.jsonl", "other-session")

    run_hook(bundle, env, hook_payload("wanted-session", wrong))

    assert tmux_state(state_path)["splits"] == 0


def test_split_failure_does_not_title_focus_or_enable_mouse(runtime):
    bundle, env, state_path = runtime
    transcript = rollout(Path(env["CODEX_HOME"]) / "sessions" / "root.jsonl", "failed")
    env["FAKE_TMUX_FAIL"] = "split-window"

    run_hook(bundle, env, hook_payload("failed", transcript))

    calls = [command_args(entry) for entry in tmux_log(env)]
    assert tmux_state(state_path)["splits"] == 0
    assert not any(call[:1] == ["select-pane"] for call in calls)
    assert not any(call[:1] == ["set"] for call in calls)


def test_ancestor_tmux_environment_is_used(runtime):
    bundle, env, _ = runtime
    binaries = Path(env["PATH"].split(os.pathsep, 1)[0])
    executable(binaries / "ps", FAKE_PS)
    env.pop("TMUX_PANE")
    env.pop("TMUX")
    env["FAKE_PS_PANE"] = "%9"
    env["FAKE_PS_TMUX"] = "/tmp/ancestor.sock,44,0"
    transcript = rollout(Path(env["CODEX_HOME"]) / "sessions" / "root.jsonl", "ancestor")

    run_hook(bundle, env, hook_payload("ancestor", transcript))

    split = next(entry for entry in tmux_log(env) if "split-window" in entry)
    assert split[:2] == ["-S", "/tmp/ancestor.sock"]
    assert "%9" in split


def test_nearest_process_pane_host_wins_over_stale_environment(runtime):
    bundle, env, state_path = runtime
    transcript = rollout(
        Path(env["CODEX_HOME"]) / "sessions" / "herdr.jsonl",
        "herdr-host",
    )
    env["FAKE_PS_CHAIN"] = json.dumps([
        "pane_lifecycle",
        "codex",
        "/bin/zsh",
        "/opt/homebrew/bin/herdr server",
    ])

    run_hook(bundle, env, hook_payload("herdr-host", transcript))

    assert any(entry["args"][:2] == ["pane", "split"] for entry in herdr_log(env))
    assert tmux_state(state_path)["splits"] == 0

    herdr_state = Path(env["FAKE_HERDR_STATE"])
    state = json.loads(herdr_state.read_text(encoding="utf-8"))
    state["panes"].pop("w2:p1")
    herdr_state.write_text(json.dumps(state), encoding="utf-8")
    invalid = rollout(
        Path(env["CODEX_HOME"]) / "sessions" / "invalid.jsonl",
        "invalid-herdr",
    )

    run_hook(bundle, env, hook_payload("invalid-herdr", invalid))

    assert sum(
        entry["args"][:2] == ["pane", "split"] for entry in herdr_log(env)
    ) == 1
    assert tmux_state(state_path)["splits"] == 0

    env["FAKE_PS_CHAIN"] = json.dumps([
        "pane_lifecycle",
        "codex",
        "/bin/zsh",
        "tmux: server",
    ])
    nested = rollout(
        Path(env["CODEX_HOME"]) / "sessions" / "nested.jsonl",
        "nested-tmux",
    )

    run_hook(bundle, env, hook_payload("nested-tmux", nested))

    assert tmux_state(state_path)["splits"] == 1


def test_selected_herdr_host_uses_complete_ancestry_address_over_stale_environment(runtime):
    bundle, env, _ = runtime
    env["FAKE_PS_CHAIN"] = json.dumps([
        "pane_lifecycle",
        "codex",
        "/bin/zsh",
        "/opt/homebrew/bin/herdr server",
    ])
    env["HERDR_PANE_ID"] = "w2:p1"
    env["HERDR_SOCKET_PATH"] = "/tmp/stale-herdr.sock"
    env["FAKE_PS_ENVIRONMENTS"] = json.dumps({
        "9000": {
            "HERDR_PANE_ID": "w2:p9",
            "HERDR_SOCKET_PATH": "/tmp/actual-herdr.sock",
        },
    })
    state_path = Path(env["FAKE_HERDR_STATE"])
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["panes"]["w2:p9"] = {
        "pane_id": "w2:p9",
        "label": "actual origin",
        "command": "",
    }
    state_path.write_text(json.dumps(state), encoding="utf-8")
    transcript = rollout(
        Path(env["CODEX_HOME"]) / "sessions" / "herdr-address.jsonl",
        "herdr-address",
    )

    run_hook(bundle, env, hook_payload("herdr-address", transcript))

    calls = herdr_log(env)
    assert calls[0]["socket"] == "/tmp/actual-herdr.sock"
    assert calls[0]["args"] == ["pane", "current", "--pane", "w2:p9"]


def test_selected_tmux_host_uses_complete_ancestry_address_over_stale_environment(runtime):
    bundle, env, _ = runtime
    env["TMUX_PANE"] = "%1"
    env["TMUX"] = "/tmp/stale-tmux.sock,123,0"
    env["FAKE_PS_ENVIRONMENTS"] = json.dumps({
        "9000": {
            "TMUX_PANE": "%9",
            "TMUX": "/tmp/actual-tmux.sock,44,0",
        },
    })
    transcript = rollout(
        Path(env["CODEX_HOME"]) / "sessions" / "tmux-address.jsonl",
        "tmux-address",
    )

    run_hook(bundle, env, hook_payload("tmux-address", transcript))

    split = next(entry for entry in tmux_log(env) if "split-window" in entry)
    assert split[:2] == ["-S", "/tmp/actual-tmux.sock"]
    assert "%9" in split


def test_herdr_first_subagent_opens_titled_side_pane(runtime):
    bundle, env, _ = runtime
    env["FAKE_PS_CHAIN"] = json.dumps([
        "pane_lifecycle",
        "codex",
        "/bin/zsh",
        "/opt/homebrew/bin/herdr server",
    ])
    session_id = "herdr-session"
    transcript = rollout(
        Path(env["CODEX_HOME"]) / "sessions" / "herdr exact.jsonl",
        session_id,
    )

    run_hook(bundle, env, hook_payload(session_id, transcript))

    calls = herdr_log(env)
    assert all(entry["socket"] == "/tmp/herdr.sock" for entry in calls)
    assert calls[0]["args"] == ["pane", "current", "--pane", "w2:p1"]
    assert calls[1]["args"] == [
        "pane", "split", "w2:p1", "--direction", "right",
        "--ratio", "0.4", "--no-focus",
    ]
    assert calls[2]["args"] == [
        "pane", "rename", "w2:p2", f"quakepro:codex:{session_id}",
    ]
    assert_app_launch(
        calls[3]["args"][3],
        bundle,
        "--provider",
        "codex",
        "--file",
        str(transcript),
    )
    assert calls[3]["args"][:3] == ["pane", "run", "w2:p2"]
    assert not any(entry["args"][:2] == ["pane", "focus"] for entry in calls)


def test_herdr_pane_lifecycle_deduplicates_closes_and_cleans_up(runtime):
    bundle, env, _ = runtime
    env["FAKE_PS_CHAIN"] = json.dumps([
        "pane_lifecycle",
        "codex",
        "/bin/zsh",
        "/opt/homebrew/bin/herdr server",
    ])
    env["FAKE_HERDR_SPLIT_DELAY"] = "0.2"
    session_id = "herdr-lifecycle"
    transcript = rollout(
        Path(env["CODEX_HOME"]) / "sessions" / "lifecycle.jsonl",
        session_id,
    )
    command = hook_command()
    data = json.dumps(hook_payload(session_id, transcript))
    processes = [
        subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        for _ in range(2)
    ]
    outputs = [process.communicate(data, timeout=8) for process in processes]

    assert all(process.returncode == 0 for process in processes)
    assert outputs == [("", ""), ("", "")]
    assert sum(
        entry["args"][:2] == ["pane", "split"] for entry in herdr_log(env)
    ) == 1

    run_hook(bundle, env, {
        "hook_event_name": "SessionEnd",
        "session_id": session_id,
    })

    calls = herdr_log(env)
    assert ["pane", "close", "w2:p2"] in [entry["args"] for entry in calls]

    env["FAKE_HERDR_FAIL"] = "run"
    failed_id = "herdr-failed"
    failed = rollout(
        Path(env["CODEX_HOME"]) / "sessions" / "failed.jsonl",
        failed_id,
    )
    run_hook(bundle, env, hook_payload(failed_id, failed))

    state = json.loads(Path(env["FAKE_HERDR_STATE"]).read_text(encoding="utf-8"))
    assert set(state["panes"]) == {"w2:p1"}


def test_session_end_closes_matching_tmux_pane(runtime):
    bundle, env, state_path = runtime
    session_id = "closed-session"
    title = f"quakepro:codex:{session_id}"
    state_path.write_text(
        json.dumps({"titles": [title], "splits": 0}),
        encoding="utf-8",
    )
    socket_path = Path("/tmp") / f"qp-{os.getpid()}-{time.time_ns()}.sock"
    tmux_socket = socket.socket(socket.AF_UNIX)
    tmux_socket.bind(str(socket_path))
    env["TMUX"] = f"{socket_path},123,0"
    env["FAKE_PS_TMUX"] = f"{socket_path},123,0"

    try:
        run_hook(bundle, env, {
            "hook_event_name": "SessionEnd",
            "session_id": session_id,
        })
    finally:
        tmux_socket.close()
        socket_path.unlink(missing_ok=True)

    calls = [command_args(entry) for entry in tmux_log(env)]
    assert ["kill-pane", "-t", "%2"] in calls


def test_complete_tmux_ancestor_address_wins_over_partial_environment(runtime):
    bundle, env, _ = runtime
    binaries = Path(env["PATH"].split(os.pathsep, 1)[0])
    executable(binaries / "ps", FAKE_PS)
    env["TMUX_PANE"] = "%7"
    env.pop("TMUX")
    env["FAKE_PS_PANE"] = "%9"
    env["FAKE_PS_TMUX"] = "/tmp/recovered.sock,44,0"
    transcript = rollout(Path(env["CODEX_HOME"]) / "sessions" / "root.jsonl", "partial")

    run_hook(bundle, env, hook_payload("partial", transcript))

    split = next(entry for entry in tmux_log(env) if "split-window" in entry)
    assert split[:2] == ["-S", "/tmp/recovered.sock"]
    assert "%9" in split


def test_unresolved_partial_tmux_environment_does_not_open_terminal(runtime):
    bundle, env, state_path = runtime
    env["TMUX_PANE"] = "%7"
    env.pop("TMUX")
    env["FAKE_PS_TMUX"] = ""
    env["FAKE_UNAME"] = "Darwin"
    transcript = rollout(Path(env["CODEX_HOME"]) / "sessions" / "root.jsonl", "partial")

    run_hook(bundle, env, hook_payload("partial", transcript))

    assert tmux_state(state_path)["splits"] == 0
    assert not Path(env["FAKE_OSASCRIPT_LOG"]).exists()


@pytest.mark.parametrize("failure, delay", [
    ("list-panes", "0"),
    ("", "2"),
])
def test_list_panes_failure_or_timeout_fails_closed(runtime, failure, delay):
    bundle, env, state_path = runtime
    env["FAKE_TMUX_FAIL"] = failure
    env["FAKE_LIST_DELAY"] = delay
    transcript = rollout(Path(env["CODEX_HOME"]) / "sessions" / "root.jsonl", "list-fail")

    run_hook(bundle, env, hook_payload("list-fail", transcript))

    assert tmux_state(state_path)["splits"] == 0
    assert not any("split-window" in entry for entry in tmux_log(env))


def test_lock_wait_stops_at_overall_deadline(runtime):
    bundle, env, state_path = runtime
    session_id = "locked-session"
    transcript = rollout(Path(env["CODEX_HOME"]) / "sessions" / "root.jsonl", session_id)
    env["QUAKEPRO_HOOK_BUDGET"] = "0.35"
    lock_root = Path(env["QUAKEPRO_STATE_DIR"])
    lock_root.mkdir(parents=True, mode=0o700)
    lock_path = lock_root / f"codex-{session_id}.lock"
    with lock_path.open("w") as lock:
        lock_path.chmod(0o600)
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        started = time.monotonic()
        run_hook(bundle, env, hook_payload(session_id, transcript))
        elapsed = time.monotonic() - started

    assert elapsed < 1.0
    assert tmux_state(state_path)["splits"] == 0


def test_slow_ancestor_command_stops_at_overall_deadline(runtime):
    bundle, env, state_path = runtime
    binaries = Path(env["PATH"].split(os.pathsep, 1)[0])
    executable(binaries / "ps", FAKE_PS)
    env.pop("TMUX_PANE")
    env.pop("TMUX")
    env["FAKE_PS_DELAY"] = "2"
    env["QUAKEPRO_HOOK_BUDGET"] = "0.35"
    transcript = rollout(Path(env["CODEX_HOME"]) / "sessions" / "root.jsonl", "slow-ps")

    started = time.monotonic()
    run_hook(bundle, env, hook_payload("slow-ps", transcript))
    elapsed = time.monotonic() - started

    assert elapsed < 1.0
    assert tmux_state(state_path)["splits"] == 0


def test_slow_title_is_bounded_and_split_self_identifies(runtime):
    bundle, env, _ = runtime
    session_id = "slow-title"
    transcript = rollout(Path(env["CODEX_HOME"]) / "sessions" / "root.jsonl", session_id)
    env["FAKE_TITLE_DELAY"] = "2"
    env["QUAKEPRO_HOOK_BUDGET"] = "4"

    started = time.monotonic()
    run_hook(bundle, env, hook_payload(session_id, transcript))
    elapsed = time.monotonic() - started

    calls = [command_args(entry) for entry in tmux_log(env)]
    split = next(call for call in calls if call[:1] == ["split-window"])
    assert elapsed < 4.5
    assert f"-T {shlex.quote(f'quakepro:codex:{session_id}')}" in split[-1]
    assert any(call[:1] == ["kill-pane"] for call in calls)


@pytest.mark.parametrize("platform", ["Darwin", "Linux"])
@pytest.mark.parametrize("iterm", ["true", "false"])
def test_without_pane_host_never_opens_terminal(runtime, platform, iterm):
    bundle, env, state_path = runtime
    env.pop("TMUX_PANE")
    env.pop("TMUX")
    env["FAKE_PS_CHAIN"] = json.dumps(["pane_lifecycle", "codex", "/bin/zsh"])
    env["FAKE_UNAME"] = platform
    env["FAKE_ITERM_EXISTS"] = iterm
    transcript = rollout(Path(env["CODEX_HOME"]) / "sessions" / "root.jsonl", "no-host")

    run_hook(bundle, env, hook_payload("no-host", transcript))
    run_hook(bundle, env, hook_payload("no-host", transcript))

    assert tmux_state(state_path)["splits"] == 0
    assert not Path(env["FAKE_OSASCRIPT_LOG"]).exists()
