# QuakePro

QuakePro observes agent sessions and presents their current structure and activity.

## Language

**Session**:
One parent agent run and all descendant agents, workflows, teams, and tasks connected to it.

**Observation signal**:
A notice that a session may have changed. It wakes reconciliation but does not itself define displayed state.
_Avoid_: Poll, tick

**Lifecycle fact**:
An authoritative notice that a session or agent started, stopped, or ended.
_Avoid_: Timeout, silence

**Session projection**:
Current read-only view of a session: agent tree, action timeline, run tabs, and task board.
_Avoid_: Model, snapshot

**Dispatch**:
One same-parent sequence of Codex child-agent launches with no parent work between launches,
Claude Task and Agent launches from one assistant message, or pi children carried by one
subagent result. Spawn results and child activity notices may occur inside Codex sequence.
_Avoid_: Batch inferred from elapsed time

**Subagent role**:
One primary purpose for a subagent run, matched from visible agent name.
_Avoid_: Current action, workflow phase
