import pytest
from rich.cells import cell_len

from quakepro.agent_roles import ROLE_ICONS, ROLE_STYLES, classify_role, role_style
from quakepro.session_model import Node, Step


@pytest.mark.parametrize(
    ("purpose", "role"),
    (
        ("primary_source_research", "research"),
        ("anthropic_json_sources", "other"),
        ("researcher", "research"),
        ("parser_api_design", "design"),
        ("planner", "design"),
        ("implement_parser", "code"),
        ("builder", "code"),
        ("showcase_terra_impl", "code"),
        ("dev_owner", "coordinate"),
        ("source_code_builder", "code"),
        ("fix_parser", "code"),
        ("hotfix_parser", "code"),
        ("bugfix_worker", "code"),
        ("docset_correction_owner", "code"),
        ("showcase_terra_tests", "tests"),
        ("t037_tests_fast", "tests"),
        ("qa_worker", "tests"),
        ("benchmark", "tests"),
        ("review3_3703", "review"),
        ("prefix_review", "review"),
        ("rereview_3703", "review"),
        ("t048_conformance_blind_r4", "review"),
        ("t048_quality_blind_r4", "review"),
        ("quality_of_service_owner", "coordinate"),
        ("t082_terminal_falsifier", "review"),
        ("diagnose_parser", "debug"),
        ("triage_worker", "debug"),
        ("parser_docs", "docs"),
        ("showcase_tree_supervisor", "coordinate"),
        ("owner_3702", "coordinate"),
        ("fixture_owner", "coordinate"),
        ("device_owner", "coordinate"),
        ("qatar_owner", "coordinate"),
        ("triangular_owner", "coordinate"),
    ),
)
def test_classify_subagent_roles_from_name(purpose, role):
    node = Node("child", purpose, parent="main")

    assert classify_role(node) == role


def test_fix_keywords_beat_review_while_plain_code_does_not():
    assert classify_role(Node("a", "fix_quality_findings")) == "code"
    assert classify_role(Node("b", "code_quality_review")) == "review"


def test_generic_agent_type_uses_visible_detail_as_name():
    node = Node("child", "general-purpose", detail="quality reviewer")

    assert classify_role(node) == "review"


def test_role_icons_are_distinct_terminal_cells():
    assert set(ROLE_ICONS) == {
        "research", "design", "code", "tests", "review",
        "debug", "docs", "coordinate", "other",
    }
    assert len(set(ROLE_ICONS.values())) == len(ROLE_ICONS)
    assert all(cell_len(icon) == 1 for icon in ROLE_ICONS.values())
    assert ROLE_ICONS["code"] == "⌨"


def test_role_styles_cover_every_role_and_fallback():
    assert set(ROLE_STYLES) == set(ROLE_ICONS)
    assert len(set(ROLE_STYLES.values())) == len(ROLE_STYLES)
    assert role_style("research") == "bright_blue"
    assert role_style("unexpected") == ROLE_STYLES["other"]


def test_role_styles_use_ansi_palette():
    assert ROLE_STYLES == {
        "research": "bright_blue",
        "design": "medium_turquoise",
        "code": "pale_turquoise1",
        "tests": "orchid1",
        "review": "bright_yellow",
        "debug": "bright_red",
        "docs": "bright_white",
        "coordinate": "bright_magenta",
        "other": "light_slate_blue",
    }
    assert all("#" not in style for style in ROLE_STYLES.values())


def test_skills_prompts_and_actions_do_not_assign_role():
    node = Node(
        "worker", "worker_3702", parent="main",
        skills=["review-conformance"],
        steps=[
            Step("prompt", "Prompt", body="Review parser"),
            Step("tool", "apply_patch", body="tests/test_parser.py"),
            Step("tool", "exec_command", body="uv run pytest"),
            Step("spawn", "Agent", body="Coordinate parser"),
        ],
    )

    assert classify_role(node) == "other"
