import json
from pathlib import Path
import shlex
import stat

import pytest

import quakepro.codex_hooks as codex_hooks


def hook_command(root: Path) -> str:
    return shlex.join([str(root / "quakepro"), "_hook", "codex"])


def owned_handlers(document, event=None):
    return [
        handler
        for name in (codex_hooks.EVENTS if event is None else (event,))
        for group in document.get("hooks", {}).get(name, [])
        for handler in group["hooks"]
        if codex_hooks._owned(handler)
    ]


def test_install_creates_canonical_hook_with_secure_mode(tmp_path):
    command = hook_command(tmp_path / "Quake Pro's bundle")
    target = tmp_path / "codex home" / "hooks.json"

    assert codex_hooks._update(target, "install", command)

    document = json.loads(target.read_text(encoding="utf-8"))
    assert set(document["hooks"]) == set(codex_hooks.EVENTS)
    assert len(owned_handlers(document)) == len(codex_hooks.EVENTS)
    groups = document["hooks"]["SubagentStart"]
    assert len(groups) == 1
    assert "matcher" not in groups[0]
    handler = groups[0]["hooks"][0]
    assert handler == {
        "type": "command",
        "command": handler["command"],
        "timeout": 10,
        "statusMessage": "Opening QuakePro panel",
    }
    assert "async" not in handler
    assert shlex.split(handler["command"]) == [
        *shlex.split(command),
        "--quakepro-hook-id",
        "quakepro.codex-auto-open.v1",
    ]
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert stat.S_IMODE(target.parent.stat().st_mode) & 0o077 == 0


def test_install_preserves_unrelated_config_and_is_idempotent(tmp_path):
    old_script = hook_command(tmp_path / "old")
    new_script = hook_command(tmp_path / "new")
    target = tmp_path / "hooks.json"
    unrelated = {"type": "command", "command": "echo unrelated", "timeout": 7}
    document = {
        "version": 3,
        "hooks": {
            "Stop": [{"hooks": [unrelated]}],
            "SubagentStart": [
                {"matcher": "reviewer", "hooks": [{"type": "command", "command": "echo reviewer"}]},
                {
                    "hooks": [
                        codex_hooks._handler(old_script),
                        {"type": "command", "command": "echo keep"},
                    ],
                    "extension": True,
                },
            ],
        },
    }
    target.write_text(json.dumps(document), encoding="utf-8")

    assert codex_hooks._update(target, "install", new_script)
    result = json.loads(target.read_text(encoding="utf-8"))
    assert result["version"] == 3
    assert result["hooks"]["Stop"][0]["hooks"][0] == unrelated
    assert len(owned_handlers(result, "Stop")) == 1
    assert result["hooks"]["SubagentStart"][0] == document["hooks"]["SubagentStart"][0]
    assert result["hooks"]["SubagentStart"][1]["extension"] is True
    assert result["hooks"]["SubagentStart"][1]["hooks"][1]["command"] == "echo keep"
    assert len(owned_handlers(result)) == len(codex_hooks.EVENTS)
    assert all(
        shlex.split(handler["command"])[:3] == shlex.split(new_script)
        for handler in owned_handlers(result)
    )

    installed = target.read_bytes()
    assert not codex_hooks._update(target, "install", new_script)
    assert target.read_bytes() == installed


def test_install_moves_and_deduplicates_owned_handlers_only(tmp_path):
    old_script = hook_command(tmp_path / "old")
    new_script = hook_command(tmp_path / "new")
    target = tmp_path / "hooks.json"
    substring = {
        "type": "command",
        "command": "echo quakepro.codex-auto-open.v1",
    }
    document = {
        "hooks": {
            "SubagentStart": [
                {"matcher": "worker", "hooks": [codex_hooks._handler(old_script)]},
                {"hooks": [substring, codex_hooks._handler(old_script)]},
            ]
        }
    }
    target.write_text(json.dumps(document), encoding="utf-8")

    codex_hooks._update(target, "install", new_script)

    result = json.loads(target.read_text(encoding="utf-8"))
    assert len(owned_handlers(result, "SubagentStart")) == 1
    assert substring in result["hooks"]["SubagentStart"][0]["hooks"]
    assert all("matcher" not in group for group in result["hooks"]["SubagentStart"])


def test_uninstall_removes_only_owned_and_prunes_newly_empty_containers(tmp_path):
    script = hook_command(tmp_path / "bundle")
    target = tmp_path / "hooks.json"
    original = {"metadata": {"keep": True}}
    target.write_text(json.dumps(original), encoding="utf-8")
    codex_hooks._update(target, "install", script)

    assert codex_hooks._update(target, "uninstall", script)
    assert json.loads(target.read_text(encoding="utf-8")) == original
    absent = target.read_bytes()
    assert not codex_hooks._update(target, "uninstall", script)
    assert target.read_bytes() == absent


def test_uninstall_preserves_unrelated_group_and_empty_event(tmp_path):
    script = hook_command(tmp_path / "bundle")
    target = tmp_path / "hooks.json"
    unrelated = {"type": "command", "command": "echo keep"}
    document = {
        "hooks": {
            "SubagentStart": [{"hooks": [unrelated, codex_hooks._handler(script)]}],
            "Stop": [],
        }
    }
    target.write_text(json.dumps(document), encoding="utf-8")

    codex_hooks._update(target, "uninstall", script)

    result = json.loads(target.read_text(encoding="utf-8"))
    assert result == {"hooks": {"SubagentStart": [{"hooks": [unrelated]}]}}


@pytest.mark.parametrize(
    "content",
    [
        b"not json\n",
        b"[]\n",
        b'{"hooks": null}\n',
        b'{"hooks": []}\n',
        b'{"hooks": {"SubagentStart": {}}}\n',
        b'{"hooks": {"SubagentStart": [{}]}}\n',
        b'{"hooks": {"SubagentStart": [{"hooks": ["bad"]}]}}\n',
    ],
)
def test_malformed_or_wrong_shape_config_is_untouched(tmp_path, content):
    script = hook_command(tmp_path / "bundle")
    target = tmp_path / "hooks.json"
    target.write_bytes(content)

    with pytest.raises(codex_hooks.HookConfigError):
        codex_hooks._update(target, "install", script)

    assert target.read_bytes() == content


def test_atomic_write_restricts_existing_mode(tmp_path):
    script = hook_command(tmp_path / "bundle")
    target = tmp_path / "hooks.json"
    target.write_text("{}\n", encoding="utf-8")
    target.chmod(0o640)

    codex_hooks._update(target, "install", script)

    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_atomic_replace_failure_preserves_original_and_cleans_temp(tmp_path, monkeypatch):
    script = hook_command(tmp_path / "bundle")
    target = tmp_path / "config" / "hooks.json"
    target.parent.mkdir()
    original = b'{"keep": true}\n'
    target.write_bytes(original)

    def fail_replace(source, destination):
        raise OSError("replace failed")

    monkeypatch.setattr(codex_hooks.os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        codex_hooks._update(target, "install", script)

    assert target.read_bytes() == original
    assert list(target.parent.glob(".hooks.json.*")) == []


def test_symlink_target_is_replaced_without_changing_referent(tmp_path):
    script = hook_command(tmp_path / "bundle")
    referent = tmp_path / "shared.json"
    referent.write_text("{}\n", encoding="utf-8")
    target = tmp_path / "hooks.json"
    target.symlink_to(referent)

    codex_hooks._update(target, "install", script)

    assert not target.is_symlink()
    assert referent.read_text(encoding="utf-8") == "{}\n"
    assert len(owned_handlers(json.loads(target.read_text(encoding="utf-8")))) == len(
        codex_hooks.EVENTS
    )


def test_legacy_hook_script_runtime_validation(tmp_path):
    root = tmp_path / "bundle"
    script = root / "codex-open-pane.sh"
    with pytest.raises(codex_hooks.HookConfigError, match="runtime is incomplete"):
        codex_hooks._validate_runtime(script)

    script.parent.mkdir()
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    script.chmod(0o755)
    package = root / "src" / "quakepro"
    package.mkdir(parents=True)
    for name in (
        "__init__.py",
        "app.py",
        "pane_lifecycle.py",
        "lifecycle.py",
        "session_model.py",
        "bounded_json.py",
        "provider_admission.py",
        "provider_schema.py",
        "session_identity.py",
        "source_cohort.py",
    ):
        (package / name).write_text("", encoding="utf-8")
    python = root / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/sh\n", encoding="utf-8")
    python.chmod(0o755)

    codex_hooks._validate_runtime(script)


@pytest.mark.parametrize("configured", ["", "   \t"])
def test_empty_codex_home_uses_home_default(tmp_path, monkeypatch, configured):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", configured)

    hooks, config = codex_hooks._paths("user")

    assert hooks == home / ".codex" / "hooks.json"
    assert config == home / ".codex" / "config.toml"


def test_project_scope_resolves_nested_cwd_to_repository_root(tmp_path, monkeypatch):
    repository = tmp_path / "repository"
    (repository / ".git").mkdir(parents=True)
    nested = repository / "src" / "package"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "user-codex"))

    hooks, config = codex_hooks._paths("project")

    assert hooks == repository / ".codex" / "hooks.json"
    assert config == repository / ".codex" / "config.toml"


def test_project_scope_refuses_non_repository(tmp_path, monkeypatch):
    directory = tmp_path / "not-a-repository"
    directory.mkdir()
    monkeypatch.chdir(directory)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "user-codex"))

    with pytest.raises(codex_hooks.HookConfigError, match="cannot find a Git project"):
        codex_hooks._paths("project")


def test_project_scope_refuses_user_target_alias(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".git").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.chdir(home)

    with pytest.raises(codex_hooks.HookConfigError, match="aliases the user"):
        codex_hooks._paths("project")


def test_project_scope_refuses_explicit_codex_home_alias(tmp_path, monkeypatch):
    repository = tmp_path / "repository"
    (repository / ".git").mkdir(parents=True)
    monkeypatch.setenv("CODEX_HOME", str(repository / ".codex"))
    monkeypatch.chdir(repository)

    with pytest.raises(codex_hooks.HookConfigError, match="aliases the user"):
        codex_hooks._paths("project")


def test_explicit_project_root_and_scope_validation(tmp_path, monkeypatch):
    repository = tmp_path / "repository"
    (repository / ".git").mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "user-codex"))

    hooks, _ = codex_hooks._paths("project", str(repository))

    assert hooks == repository / ".codex" / "hooks.json"
    with pytest.raises(codex_hooks.HookConfigError, match="requires --scope project"):
        codex_hooks._paths("user", str(repository))
    with pytest.raises(codex_hooks.HookConfigError, match="not a Git repository"):
        codex_hooks._paths("project", str(elsewhere))
