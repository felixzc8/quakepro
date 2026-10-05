import json
import shlex

import pytest

import quakepro.app as app
import quakepro.cli as cli
import quakepro.pane_lifecycle as pane_lifecycle


def test_cli_reports_installed_version(capsys):
    assert cli.main(["--version"]) == 0

    assert capsys.readouterr().out == "quakepro 0.1.0\n"


def test_cli_routes_session_arguments_to_app(monkeypatch):
    received = []
    monkeypatch.setattr(app, "main", lambda argv=None: received.append(argv) or 0)

    assert cli.main(["--provider", "codex"]) == 0

    assert received == [["--provider", "codex"]]


@pytest.mark.parametrize("command", ("hooks", "setup", "doctor"))
def test_cli_config_help_does_not_resolve_executable(
        command, capsys, monkeypatch):
    def unexpected_resolution():
        raise AssertionError("resolved executable during help")

    monkeypatch.setattr(cli, "_executable", unexpected_resolution)

    with pytest.raises(SystemExit) as stopped:
        cli.main([command, "--help"])

    assert stopped.value.code == 0
    assert f"usage: quakepro {command}" in capsys.readouterr().out


def test_cli_routes_internal_hook_event_through_installed_executable(
        tmp_path, monkeypatch):
    executable = tmp_path / "quakepro"
    executable.write_text("installed\n", encoding="utf-8")
    executable.chmod(0o755)
    monkeypatch.setenv("QUAKEPRO_EXECUTABLE", str(executable))
    received = []
    monkeypatch.setattr(
        pane_lifecycle,
        "main",
        lambda argv=None, executable=None: received.append((argv, executable)) or 0,
    )

    assert cli.main(["_hook", "claude", "--quakepro-hook-id", "owned"]) == 0

    assert received == [(
        ["claude", "--quakepro-hook-id", "owned"], str(executable),
    )]


def test_setup_all_installs_provider_hooks(tmp_path, monkeypatch, capsys):
    executable = tmp_path / "bin" / "quakepro"
    executable.parent.mkdir()
    executable.write_text("installed\n", encoding="utf-8")
    executable.chmod(0o755)
    codex_home = tmp_path / "codex"
    claude_home = tmp_path / "claude"
    pi_home = tmp_path / "pi"
    claude_home.mkdir()
    claude_settings = claude_home / "settings.json"
    claude_settings.write_text(
        json.dumps({"permissions": {"allow": ["Read"]}}), encoding="utf-8",
    )
    monkeypatch.setenv("QUAKEPRO_EXECUTABLE", str(executable))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_home))
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(pi_home))

    assert cli.main(["setup", "--provider", "all"]) == 0

    codex = json.loads((codex_home / "hooks.json").read_text(encoding="utf-8"))
    claude = json.loads(claude_settings.read_text(encoding="utf-8"))
    assert claude["permissions"] == {"allow": ["Read"]}
    for provider, document in (("codex", codex), ("claude", claude)):
        commands = [
            handler["command"]
            for groups in document["hooks"].values()
            for group in groups
            for handler in group["hooks"]
            if "quakepro-hook-id" in handler.get("command", "")
        ]
        assert len(commands) == 6
        assert all(shlex.split(command)[:3] == [
            str(executable), "_hook", provider,
        ] for command in commands)
    claude_start = claude["hooks"]["SubagentStart"][0]["hooks"][0]
    assert claude_start["async"] is True
    assert claude_start["statusMessage"] == "Opening QuakePro panel"
    output = capsys.readouterr().out
    assert f"Installed Claude hooks: {claude_settings}" in output
    assert f"Installed Codex hooks: {codex_home / 'hooks.json'}" in output
    assert f"Installed Pi hooks: {pi_home / 'extensions' / 'quakepro.js'}" in output
    assert "Restart Claude Code" in output
    assert "open /hooks to review and trust" in output
    assert "Restart pi or run /reload" in output
    assert claude_settings.stat().st_mode & 0o777 == 0o600

    before = (
        claude_settings.read_bytes(),
        (codex_home / "hooks.json").read_bytes(),
        (pi_home / "extensions" / "quakepro.js").read_bytes(),
    )
    assert cli.main(["setup", "--provider", "all"]) == 0
    assert before == (
        claude_settings.read_bytes(), (codex_home / "hooks.json").read_bytes(),
        (pi_home / "extensions" / "quakepro.js").read_bytes(),
    )


def test_setup_leaves_malformed_claude_settings_unchanged(
        tmp_path, monkeypatch, capsys):
    executable = tmp_path / "quakepro"
    executable.write_text("installed\n", encoding="utf-8")
    executable.chmod(0o755)
    claude_home = tmp_path / "claude"
    claude_home.mkdir()
    settings = claude_home / "settings.json"
    settings.write_bytes(b"broken")
    monkeypatch.setenv("QUAKEPRO_EXECUTABLE", str(executable))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_home))

    assert cli.main(["setup", "--provider", "claude"]) == 1

    assert settings.read_bytes() == b"broken"
    assert str(settings) in capsys.readouterr().err


def test_setup_leaves_wrong_claude_hook_shape_unchanged(
        tmp_path, monkeypatch, capsys):
    executable = tmp_path / "quakepro"
    executable.write_text("installed\n", encoding="utf-8")
    executable.chmod(0o755)
    claude_home = tmp_path / "claude"
    claude_home.mkdir()
    settings = claude_home / "settings.json"
    settings.write_text('{"hooks": null}\n', encoding="utf-8")
    monkeypatch.setenv("QUAKEPRO_EXECUTABLE", str(executable))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_home))

    assert cli.main(["setup", "--provider", "claude"]) == 1

    assert settings.read_bytes() == b'{"hooks": null}\n'
    assert "top-level 'hooks' value" in capsys.readouterr().err


def test_claude_hook_uninstall_preserves_unrelated_configuration(
        tmp_path, monkeypatch):
    executable = tmp_path / "quakepro"
    executable.write_text("installed\n", encoding="utf-8")
    executable.chmod(0o755)
    claude_home = tmp_path / "claude"
    claude_home.mkdir()
    settings = claude_home / "settings.json"
    original = {
        "theme": "dark",
        "hooks": {
            "SessionStart": [{
                "matcher": "startup",
                "hooks": [{
                    "type": "command",
                    "command": "keep-me",
                    "timeout": 2,
                }],
            }],
        },
    }
    settings.write_text(json.dumps(original), encoding="utf-8")
    monkeypatch.setenv("QUAKEPRO_EXECUTABLE", str(executable))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_home))

    assert cli.main(["hooks", "install", "--provider", "claude"]) == 0
    assert cli.main(["hooks", "uninstall", "--provider", "claude"]) == 0

    assert json.loads(settings.read_text(encoding="utf-8")) == original


def test_doctor_verifies_hooks_without_changing_configuration(
        tmp_path, monkeypatch, capsys):
    executable = tmp_path / "quakepro"
    executable.write_text("installed\n", encoding="utf-8")
    executable.chmod(0o755)
    codex_home = tmp_path / "codex"
    claude_home = tmp_path / "claude"
    pi_home = tmp_path / "pi"
    monkeypatch.setenv("QUAKEPRO_EXECUTABLE", str(executable))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_home))
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(pi_home))
    assert cli.main(["setup", "--provider", "all"]) == 0
    capsys.readouterr()
    paths = (
        claude_home / "settings.json",
        codex_home / "hooks.json",
        pi_home / "extensions" / "quakepro.js",
    )
    before = tuple(path.read_bytes() for path in paths)

    assert cli.main(["doctor", "--provider", "all"]) == 0

    assert tuple(path.read_bytes() for path in paths) == before
    output = capsys.readouterr().out
    assert f"OK Claude hooks: {paths[0]}" in output
    assert f"OK Codex hooks: {paths[1]}" in output
    assert f"OK Pi hooks: {paths[2]}" in output


def test_doctor_reports_stale_hook_command(tmp_path, monkeypatch, capsys):
    executable = tmp_path / "quakepro"
    executable.write_text("installed\n", encoding="utf-8")
    executable.chmod(0o755)
    claude_home = tmp_path / "claude"
    monkeypatch.setenv("QUAKEPRO_EXECUTABLE", str(executable))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_home))
    assert cli.main(["setup", "--provider", "claude"]) == 0
    capsys.readouterr()
    settings = claude_home / "settings.json"
    document = json.loads(settings.read_text(encoding="utf-8"))
    handler = document["hooks"]["SubagentStart"][0]["hooks"][0]
    handler["command"] = handler["command"].replace(str(executable), "/old/quakepro")
    settings.write_text(json.dumps(document), encoding="utf-8")
    before = settings.read_bytes()

    assert cli.main(["doctor", "--provider", "claude"]) == 1

    assert settings.read_bytes() == before
    error = capsys.readouterr().err
    assert "Claude hooks need setup" in error
    assert str(settings) in error
