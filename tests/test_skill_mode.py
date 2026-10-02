import pytest

import quakepro.skill_mode as skill_mode


@pytest.mark.parametrize(
    ("name", "inputs", "skill"),
    [
        pytest.param("Skill", {"skill": "tdd"}, "tdd", id="claude-skill"),
        pytest.param(
            "Read", {"file_path": "/agents/skills/research/SKILL.md"},
            "research", id="claude-read",
        ),
        pytest.param(
            "read", {"path": "/home/user/.pi/agent/skills/orchestrate.md"},
            "orchestrate", id="pi-read",
        ),
        pytest.param(
            "exec_command",
            {"cmd": "sed -n '1,200p' /agents/skills/codebase-design/SKILL.md"},
            "codebase-design", id="codex-shell-read",
        ),
        pytest.param(
            "exec_command",
            {"cmd": "rg codebase-design /agents/skills/codebase-design/SKILL.md"},
            "", id="shell-mention-is-not-load",
        ),
    ],
)
def test_skill_of_recognizes_only_recorded_skill_loads(name, inputs, skill):
    assert skill_mode.skill_of(name, inputs) == skill
