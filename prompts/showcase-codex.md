# QuakePro Codex showcase

Run this exact, cheap, read-only showcase from project root.

## Hard rules

- Root may use only direct `spawn_agent`, `wait_agent`, and `send_message` calls plus
  `exec_command`.
- Agents may use only tools named in their task message.
- Collaboration tools are direct assistant tools. Never search `ALL_TOOLS` for them. Never
  call them through `functions.exec` or `exec_command`.
- Each `exec_command` call must contain one exact command listed below. Do not combine,
  prefix, suffix, or retry commands.
- Do not write files. Do not use network. Do not spawn roles not listed here.
- Make no commentary. Every assistant or agent text reply must be one allowed result line.
- Success requires every wave leaf plus full nested chain. Never infer success from a spawn.
- If required collaboration tool is absent or its call fails, use blocked result. Never fake
  tool call or completion.

Root failure mapping is exact:

- Failed or unavailable `spawn_agent` call: `result: showcase blocked: spawn ROLE failed`
- Failed or unavailable `wait_agent` call: `result: showcase blocked: wait_agent unavailable`

Replace `ROLE` with requested task name. Stop after any blocked result.

Allowed root result lines:

```text
result: showcase complete
result: showcase blocked: REASON
```

Allowed agent result lines:

```text
result: ROLE complete
result: showcase blocked: REASON
```

Allowed nested signals to `/root`:

```text
nested result: complete
nested result: blocked: REASON
```

## Fixed agent settings

Use `reasoning_effort: "low"` and `fork_turns: "none"` for every spawn. Use exact
`task_name` shown below.

| Role | Model | May spawn |
| --- | --- | --- |
| `researcher` | `gpt-5.6-luna` | no |
| `coder` | `gpt-5.6-luna` | no |
| `tester` | `gpt-5.6-luna` | no |
| `designer` | `gpt-5.6-luna` | no |
| `reviewer` | `gpt-5.6-luna` | no |
| `debugger` | `gpt-5.6-luna` | no |
| `coordinator` | `gpt-5.6-terra` | `planner` |
| `planner` | `gpt-5.6-terra` | `docs_writer` |
| `docs_writer` | `gpt-5.6-terra` | `qa_reviewer` |
| `qa_reviewer` | `gpt-5.6-terra` | `release_tester` |
| `release_tester` | `gpt-5.6-luna` | no |

Only roles that spawn child use `gpt-5.6-terra`. All leaves use `gpt-5.6-luna`.

## Fixed role commands

- `researcher`: `rg --files -g '*.md' | head -n 5`
- `coder`: `rg -n '^class ' src/quakepro/app.py src/quakepro/graph.py | head -n 5`
- `tester`: `rg -n '^def test_' tests | head -n 5`
- `designer`: `rg -n 'CSS|STYLE' src/quakepro/app.py src/quakepro/graph.py | head -n 5`
- `reviewer`: `git diff --check`
- `debugger`: `rg -n 'except|raise' src/quakepro/claude_model.py | head -n 5`
- `coordinator`: `find . -maxdepth 1 -type f -print | head -n 5`
- `planner`: `rg -n '^## ' README.md | head -n 5`
- `docs_writer`: `rg -n '\[[^]]+\]\(' README.md | head -n 5`
- `qa_reviewer`: `rg -n 'pytest' pyproject.toml README.md | head -n 5`
- `release_tester`: `rg -n '^def test_' tests/test_agent_roles.py | head -n 5`

## Leaf task contract

For each non-debugger wave leaf, use this exact task-message template. Replace only `ROLE` and
`COMMAND` with fixed table values:

```text
ROLE=ROLE
Use only exec_command. Do not write files or use network.
Run these exact commands in order, each in a separate exec_command call:
1. pwd
2. COMMAND
3. sleep 1
Do not run any other command. Do not call collaboration tools.
If any command exits nonzero, reply exactly: result: showcase blocked: ROLE command failed
Otherwise reply exactly: result: ROLE complete
```

For `debugger`, use this exact task message:

```text
ROLE=debugger
Use only exec_command. Do not write files or use network.
Run these exact commands in order, each in a separate exec_command call:
1. pwd
2. rg -n 'except|raise' src/quakepro/claude_model.py | head -n 5
3. false
4. sleep 1
Do not run any other command. Do not call collaboration tools.
Run false once and do not retry. Preserve expected failure: in code mode, after exec_command
returns nonzero, throw so enclosing tool call also fails. Run sleep 1 in next agent turn.
pwd, role command, and sleep 1 must exit zero. If any does not, reply exactly:
result: showcase blocked: debugger command failed
Otherwise reply exactly: result: debugger complete
```

## Wave 1

In one root assistant turn, make exactly three consecutive direct `spawn_agent` calls. Put no
text or tool call between them. Spawn in this order:

1. `researcher`
2. `coder`
3. `tester`

Use fixed settings and leaf task contract. After all three spawn calls return, call direct
`wait_agent` with `timeout_ms: 30000` until all three exact completion lines arrive. Do not
start Wave 2 before all three complete.

If any agent returns blocked or unexpected result, reply
`result: showcase blocked: ROLE failed` and stop. If two consecutive waits time out, reply
`result: showcase blocked: wave 1 timeout` and stop.

## Wave 2

In one root assistant turn, make exactly three consecutive direct `spawn_agent` calls. Put no
text or tool call between them. Spawn in this order:

1. `designer`
2. `reviewer`
3. `debugger`

Use fixed settings and leaf task contract. After all three spawn calls return, call direct
`wait_agent` with `timeout_ms: 30000` until all three exact completion lines arrive. Do not
start nested chain before all three complete.

If any agent returns blocked or unexpected result, reply
`result: showcase blocked: ROLE failed` and stop. If two consecutive waits time out, reply
`result: showcase blocked: wave 2 timeout` and stop.

## Nested chain

Build exactly this chain:

```text
coordinator -> planner -> docs_writer -> qa_reviewer -> release_tester
```

Root spawns only `coordinator`. Each dispatcher spawns only next role. Each dispatcher through
`docs_writer` finishes after spawn returns. `qa_reviewer` waits for `release_tester`, then
signals root. This staged handoff keeps chain within small concurrency limits.

Spawn `coordinator` with fixed settings. Its task message must contain exactly one first line,
`CURRENT_ROLE=coordinator`, followed by full packet below. Each dispatcher copies full packet
unchanged into child message and changes only first-line role value.

```text
CHAIN_PACKET_START
Use only direct spawn_agent, wait_agent, and send_message calls plus exec_command. Never search
ALL_TOOLS. Never call collaboration tools through functions.exec or exec_command. Do not write
files. Do not use network.

Rows:
coordinator | find . -maxdepth 1 -type f -print | head -n 5 | planner
planner | rg -n '^## ' README.md | head -n 5 | docs_writer
docs_writer | rg -n '\[[^]]+\]\(' README.md | head -n 5 | qa_reviewer
qa_reviewer | rg -n 'pytest' pyproject.toml README.md | head -n 5 | release_tester
release_tester | rg -n '^def test_' tests/test_agent_roles.py | head -n 5 | LEAF

Models:
planner | gpt-5.6-terra
docs_writer | gpt-5.6-terra
qa_reviewer | gpt-5.6-terra
release_tester | gpt-5.6-luna

Protocol:
1. Read CURRENT_ROLE from first task-message line. Select exactly matching row. Do not use any
   other row.
2. Run pwd, selected row command, and sleep 1 in that order as three separate exec_command
   calls. Do not run any other command.
3. Each command must exit zero. On command failure as release_tester, reply result: showcase
   blocked: release_tester command failed and stop. On command failure as any dispatcher,
   directly send_message to /root with message nested result: blocked: CURRENT_ROLE command
   failed, then reply result: showcase blocked: CURRENT_ROLE command failed and stop.
4. If selected row next value is LEAF, reply exactly result: release_tester complete and stop.
   Do not call any collaboration tool.
5. Otherwise, call spawn_agent exactly once for next value. Use next value as task_name,
   matching model from Models, reasoning_effort low, and fork_turns none. Child task message is
   one first line CURRENT_ROLE=NEXT followed by this full packet unchanged.
6. If spawn_agent is absent or spawn fails, directly send_message to /root with message nested
   result: blocked: spawn ROLE failed. Then reply result: showcase blocked: spawn ROLE failed
   and stop. Replace ROLE with next value.
7. If next value is planner, docs_writer, or qa_reviewer, reply exactly result: CURRENT_ROLE
   complete after spawn returns. Do not wait for child. Stop.
8. If next value is release_tester, current role must be qa_reviewer. Call wait_agent with
   timeout_ms 30000 until release_tester returns final result. Do not spawn anything else.
9. If exact child result is result: release_tester complete, directly send_message to /root
   with message nested result: complete. After send succeeds, reply exactly result: qa_reviewer
   complete and stop.
10. If child result is blocked, failed, or unexpected, directly send_message to /root with
   message nested result: blocked: release_tester failed. Then reply result: showcase blocked:
   release_tester failed and stop.
11. If two consecutive waits time out, directly send_message to /root with message nested
    result: blocked: release_tester timeout. Then reply result: showcase blocked:
    release_tester timeout and stop.
12. If required wait_agent or send_message is absent or its call fails, reply result: showcase
    blocked: TOOL unavailable and stop. Never claim completion.
CHAIN_PACKET_END
```

After spawning `coordinator`, root calls direct `wait_agent` with `timeout_ms: 30000` until it
has both:

1. exact `result: coordinator complete`
2. exact `nested result: complete`

Record either signal regardless of arrival order. Do not discard early nested signal. Ignore
unrelated completion updates. If coordinator returns blocked, failed, or unexpected result,
reply `result: showcase blocked: coordinator failed` and stop. If root receives
`nested result: blocked: REASON`, reply `result: showcase blocked: REASON` and stop. If two
consecutive waits time out before both success signals arrive, reply
`result: showcase blocked: nested result timeout` and stop.

After Wave 1, Wave 2, coordinator, and nested result all succeed, reply exactly:

```text
result: showcase complete
```
