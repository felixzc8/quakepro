"""Orchestrate-workflow awareness: workflow skill loads, ticket and board activity,
review dispatches, and the phase they imply. Claude Code transcripts here; the
other providers' bindings live in tests/test_codex_model.py and tests/test_pi_model.py.
"""
import quakepro.workflow as workflow
from quakepro.claude_model import ClaudeModel
from test_model import tool_turn, write_jsonl


def turn(blocks, path=None, tmp_path=None):
    p = path or (tmp_path / "session.jsonl")
    write_jsonl(p, [tool_turn(blocks)])
    m = ClaudeModel(str(p))
    m.poll()
    return m.nodes["main"]


def test_workflow_skill_load_sets_phase(tmp_path):
    node = turn([
        {"type": "tool_use", "id": "t1", "name": "Skill", "input": {"skill": "orchestrate"}},
    ], tmp_path=tmp_path)
    assert (node.skills, node.phase) == (["orchestrate"], "orchestrating")

    node = turn([
        {"type": "tool_use", "id": "t1", "name": "Skill", "input": {"skill": "to-spec"}},
        {"type": "tool_use", "id": "t2", "name": "Read",
         "input": {"file_path": "/s/skills/to-tickets/SKILL.md"}},
        {"type": "tool_use", "id": "t3", "name": "Bash",
         "input": {"command": "sed -n 1,80p /s/skills/tdd/SKILL.md"}},
    ], tmp_path=tmp_path)
    assert node.skills == ["to-spec", "to-tickets", "tdd"]
    assert node.phase == "implement"      # latest signal wins

    # A skill outside the workflow set leaves the phase alone.
    node = turn([
        {"type": "tool_use", "id": "t1", "name": "Skill", "input": {"skill": "implement"}},
        {"type": "tool_use", "id": "t2", "name": "Skill", "input": {"skill": "research"}},
    ], tmp_path=tmp_path)
    assert node.phase == "implement"

    # Only a command that reads the definition file counts as loading the skill.
    node = turn([
        {"type": "tool_use", "id": "t1", "name": "Bash",
         "input": {"command": "grep -n Status /s/skills/code-review/SKILL.md"}},
        {"type": "tool_use", "id": "t2", "name": "Bash",
         "input": {"command": "rm -rf /s/skills/tdd/SKILL.md"}},
        {"type": "tool_use", "id": "t3", "name": "Bash",
         "input": {"command": "cat > /s/skills/tdd/SKILL.md <<'EOF'"}},
    ], tmp_path=tmp_path)
    assert (node.skills, node.phase) == ([], "")

    # Read commands chained behind another program still load.
    node = turn([
        {"type": "tool_use", "id": "t1", "name": "Bash",
         "input": {"command": "wc -l /s/skills/to-spec/SKILL.md && "
                              "sed -n '1,260p' /s/skills/to-spec/SKILL.md"}},
    ], tmp_path=tmp_path)
    assert (node.skills, node.phase) == (["to-spec"], "spec")


def test_phase_comes_from_skill_and_review_signals_only(tmp_path):
    node = turn([
        {"type": "tool_use", "id": "t1", "name": "Skill", "input": {"skill": "implement"}},
        {"type": "tool_use", "id": "t2", "name": "Edit",
         "input": {"file_path": "/w/.scratch/stack/issues/03-x.md",
                   "old_string": "**Status:** ready-for-agent",
                   "new_string": "**Status:** in-progress"}},
        {"type": "tool_use", "id": "t3", "name": "Bash",
         "input": {"command": "blackboard show --project ."}},
    ], tmp_path=tmp_path)
    assert node.workflow == ["ticket 03-x → in-progress", "board show"]
    assert node.phase == "implement"      # ticket and board events are events, not phases


def test_ticket_activity_surfaces_events(tmp_path):
    node = turn([
        {"type": "tool_use", "id": "t1", "name": "Write",
         "input": {"file_path": "/w/.scratch/orchestrate-stack/issues/06-awareness.md",
                   "content": "**Status:** ready-for-agent\n"}},
        {"type": "tool_use", "id": "t2", "name": "Edit",
         "input": {"file_path": "/w/.scratch/orchestrate-stack/issues/06-awareness.md",
                   "old_string": "**Status:** ready-for-agent",
                   "new_string": "**Status:** in-progress"}},
        # An ordinary file edit is not ticket activity.
        {"type": "tool_use", "id": "t3", "name": "Edit",
         "input": {"file_path": "/w/model.py", "old_string": "a", "new_string": "b"}},
    ], tmp_path=tmp_path)
    assert node.workflow == ["ticket 06-awareness created",
                            "ticket 06-awareness → in-progress"]
    assert node.phase == ""      # a ticket says nothing about which phase runs


def test_ticket_events_need_a_ticket_path(tmp_path):
    node = turn([
        # Any file can carry a Status line; only the workflow's own path is a ticket.
        {"type": "tool_use", "id": "t1", "name": "Edit",
         "input": {"file_path": "/w/README.md", "old_string": "x",
                   "new_string": "**Status:** draft"}},
        {"type": "tool_use", "id": "t2", "name": "Write",
         "input": {"file_path": "/w/my.scratch/stack/issues/03-a.md",
                   "content": "**Status:** ready-for-agent\n"}},
        {"type": "tool_use", "id": "t3", "name": "Write",
         "input": {"file_path": "/w/.scratch/stack/issues/notes.md",
                   "content": "**Status:** ready-for-agent\n"}},
    ], tmp_path=tmp_path)
    assert node.workflow == []


def test_status_events_only_on_a_real_transition(tmp_path):
    node = turn([
        # The status line is context around the edit, not a change to it.
        {"type": "tool_use", "id": "t1", "name": "Edit",
         "input": {"file_path": "/w/.scratch/stack/issues/03-a.md",
                   "old_string": "**Status:** done\nfoo",
                   "new_string": "**Status:** done\nbar"}},
    ], tmp_path=tmp_path)
    assert node.workflow == []

    # Same rules inside a Codex patch: added lines only, per file.
    context = ("*** Begin Patch\n*** Update File: .scratch/stack/issues/03-a.md\n@@\n"
               " **Status:** in-progress\n-old body\n+new body\n*** End Patch")
    assert workflow.ticket_event("apply_patch", {"patch": context}) == ""
    cross = ("*** Begin Patch\n*** Update File: docs/plan.md\n@@\n+**Status:** shipped\n"
             "*** Update File: .scratch/stack/issues/03-a.md\n@@\n-a\n+b\n*** End Patch")
    assert workflow.ticket_event("apply_patch", {"patch": cross}) == ""
    moved = ("*** Begin Patch\n*** Update File: .scratch/stack/issues/03-a.md\n@@\n"
             "-**Status:** ready-for-agent\n+**Status:** done\n*** End Patch")
    assert workflow.ticket_event("apply_patch", {"patch": moved}) == "ticket 03-a → done"


def test_blackboard_command_surfaces_board_event(tmp_path):
    node = turn([
        {"type": "tool_use", "id": "t1", "name": "Bash",
         "input": {"command": "blackboard show --project \"$PWD\" < request.json"}},
        {"type": "tool_use", "id": "t2", "name": "Bash",
         "input": {"command": "node /w/blackboard/dist/cli.js read --project . --board b1"}},
        {"type": "tool_use", "id": "t3", "name": "Bash",
         "input": {"command": "echo blackboard is a word"}},
        {"type": "tool_use", "id": "t4", "name": "Bash",
         "input": {"command": "blackboard list --project ."}},
        # The board name inside a quoted argument is prose, not a board call.
        {"type": "tool_use", "id": "t5", "name": "Bash",
         "input": {"command": "git commit -m 'ran blackboard show for the board'"}},
        {"type": "tool_use", "id": "t6", "name": "Bash",
         "input": {"command": "grep -n 'blackboard open' notes.md"}},
    ], tmp_path=tmp_path)
    assert node.workflow == ["board show", "board read", "board list"]
    assert node.phase == ""      # a board says nothing about which phase runs


def test_review_dispatch_surfaces_and_sets_phase(tmp_path):
    node = turn([
        {"type": "tool_use", "id": "t1", "name": "Task",
         "input": {"subagent_type": "general-purpose", "description": "review 06",
                   "prompt": "Run review-falsification against the diff."}},
        {"type": "tool_use", "id": "t2", "name": "Task",
         "input": {"subagent_type": "general-purpose", "description": "write docs",
                   "prompt": "Draft the README section."}},
        # Talking about a later review is not dispatching one.
        {"type": "tool_use", "id": "t3", "name": "Task",
         "input": {"subagent_type": "general-purpose", "description": "implement 03",
                   "prompt": "Implement ticket 03. A falsification review follows later."}},
        {"type": "tool_use", "id": "t4", "name": "Task",
         "input": {"subagent_type": "general-purpose", "description": "review 07",
                   "prompt": "# Review by Falsification\n\nTarget: HEAD~1...HEAD"}},
    ], tmp_path=tmp_path)
    assert node.workflow == ["review dispatched", "review dispatched"]
    assert node.phase == "review"
