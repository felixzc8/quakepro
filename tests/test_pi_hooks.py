import json
import os
import shlex

import pytest

import pi_fixture
import quakepro.pane_lifecycle as pane_lifecycle
import quakepro.pi_hooks as pi_hooks


def test_pi_extension_install_is_owned_private_and_repeatable(tmp_path, monkeypatch):
    agent_dir = tmp_path / "pi agent"
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(agent_dir))
    executable = "/Applications/Quake Pro/bin/quakepro"

    changed, path = pi_hooks.configure("install", executable)

    assert changed
    assert path == agent_dir / "extensions" / "quakepro.js"
    text = path.read_text(encoding="utf-8")
    assert text.startswith(f"// {pi_hooks.OWNER}\n")
    assert f"const executable = {json.dumps(executable)};" in text
    assert "if (!ctx.hasUI) return;" in text
    assert "readyTranscript(current.transcriptPath, current.sessionId)" in text
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert pi_hooks.configure("install", executable) == (False, path)
    assert pi_hooks.diagnose(executable) == (path, ())


def test_pi_root_events_only_remember_session_and_subagent_events_open():
    text = pi_hooks._extension("/bin/quakepro")

    assert 'pi.on("session_start", (_event, ctx) => remember(ctx));' in text
    assert 'pi.on("session_start", (_event, ctx) => open(ctx));' not in text
    assert 'pi.on("agent_start"' not in text
    assert 'pi.on("message_start"' not in text
    assert 'if (foregroundLaunch(event)) open(ctx);' in text
    assert 'event.toolName === "subagent"' in text
    assert 'event.input.action === undefined' in text
    assert 'event.input.async !== true' in text
    assert 'pi.events.on("subagent:async-started", (payload) => asyncStart(payload));' in text
    assert 'payload.sessionId === current.sessionId' in text
    assert 'payload.sessionId === current.transcriptPath' in text
    assert 'if (!sameSession(payload, current)) return;' in text


def test_pi_extension_refresh_and_uninstall_only_owned_file(tmp_path, monkeypatch):
    agent_dir = tmp_path / "agent"
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(agent_dir))
    path = agent_dir / "extensions" / "quakepro.js"
    path.parent.mkdir(parents=True)
    path.write_text(f"// {pi_hooks.OWNER}\nstale\n", encoding="utf-8")

    assert pi_hooks.diagnose("/new/quakepro")[1] == ("stale extension",)
    assert pi_hooks.configure("install", "/new/quakepro") == (True, path)
    assert pi_hooks.configure("uninstall", "/new/quakepro") == (True, path)
    assert not path.exists()
    assert pi_hooks.configure("uninstall", "/new/quakepro") == (False, path)


def test_pi_extension_refuses_unrelated_file(tmp_path, monkeypatch):
    agent_dir = tmp_path / "agent"
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(agent_dir))
    path = agent_dir / "extensions" / "quakepro.js"
    path.parent.mkdir(parents=True)
    path.write_text("export default function () {}\n", encoding="utf-8")

    with pytest.raises(pi_hooks.HookConfigError, match="unrelated"):
        pi_hooks.configure("install", "/bin/quakepro")
    with pytest.raises(pi_hooks.HookConfigError, match="unrelated"):
        pi_hooks.configure("uninstall", "/bin/quakepro")
    assert path.read_text(encoding="utf-8") == "export default function () {}\n"


def test_pi_project_extension_uses_git_root(tmp_path, monkeypatch):
    project = tmp_path / "project"
    nested = project / "src"
    (project / ".git").mkdir(parents=True)
    nested.mkdir()
    monkeypatch.chdir(nested)
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "user-agent"))

    changed, path = pi_hooks.configure("install", "/bin/quakepro", "project")

    assert changed
    assert path == project / ".pi" / "extensions" / "quakepro.js"


def test_pi_session_start_opens_provider_specific_monitor(tmp_path, monkeypatch):
    session_id = "01a011a6-5c19-7abe-92e5-95fc1f665a0e"
    path = pi_fixture.write_session(
        tmp_path / "session.jsonl",
        [pi_fixture.header(session_id=session_id)],
    )
    opened = []
    monkeypatch.setattr(pane_lifecycle, "_pane_host", lambda: ("", [], False))
    monkeypatch.setattr(
        pane_lifecycle,
        "_open_terminal",
        lambda provider, current_id, launch: opened.append(
            (provider, current_id, shlex.split(launch)),
        ),
    )

    pane_lifecycle.handle("pi", {
        "hook_event_name": "SessionStart",
        "session_id": session_id,
        "transcript_path": path,
    }, tmp_path, "/usr/local/bin/quakepro")

    assert opened == [(
        "pi",
        session_id,
        [
            "exec",
            "/usr/local/bin/quakepro",
            "--provider",
            "pi",
            "--file",
            path,
        ],
    )]


def test_pi_hook_rejects_wrong_session_file(tmp_path, monkeypatch):
    path = pi_fixture.write_session(tmp_path / "session.jsonl", pi_fixture.entries())
    opened = []
    monkeypatch.setattr(pane_lifecycle, "_pane_host", lambda: ("", [], False))
    monkeypatch.setattr(pane_lifecycle, "_open_terminal", lambda *args: opened.append(args))

    pane_lifecycle.handle("pi", {
        "hook_event_name": "SessionStart",
        "session_id": "different",
        "transcript_path": path,
    }, tmp_path, "/usr/local/bin/quakepro")

    assert opened == []
