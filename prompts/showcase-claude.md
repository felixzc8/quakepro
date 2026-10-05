# QuakePro Claude showcase

Run this exact, cheap, read-only showcase from project root. Use only `Agent` and `Bash`.
Do not write files, use network, or use any other tool. Each Bash call must run one exact
command shown below. Keep every text reply to one short result line.

Before starting Claude Code, set `CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH=5`. Nested branch
keeps five subagent layers open at once.

For every Agent call, set `subagent_type: "general-purpose"` and `model: "haiku"`. Put role
name in Agent `description` so QuakePro can show roles. Every agent runs `pwd`, its role
command, then `sleep 1`, in separate Bash calls. Debugger also runs `false` once before
`sleep 1` and does not retry.
After its commands and any child finish, every agent replies `result: NAME complete`.

Role commands:

- `researcher`: `rg --files -g '*.md' | head -n 5`
- `coder`: `rg -n '^class ' src/quakepro/app.py src/quakepro/graph.py | head -n 5`
- `tester`: `rg -n '^def test_' tests | head -n 5`
- `designer`: `rg -n 'CSS|STYLE' src/quakepro/app.py src/quakepro/graph.py | head -n 5`
- `reviewer`: `git diff --check`
- `debugger`: `rg -n 'except|raise' src/quakepro/claude_model.py | head -n 5`
- `coordinator`: `find . -maxdepth 1 -type f -print | head -n 5`
- `planner`: `rg -n '^## ' README.md | head -n 5`
- `docs-writer`: `rg -n '\[[^]]+\]\(' README.md | head -n 5`
- `qa-reviewer`: `rg -n 'pytest' pyproject.toml README.md | head -n 5`
- `release-tester`: `rg -n '^def test_' tests/test_agent_roles.py | head -n 5`

1. In one assistant response, make exactly three concurrent Agent calls, with no text or
   other call between them:
   - description: `wave alpha researcher`; task: leaf `researcher`
   - description: `wave alpha coder`; task: leaf `coder`
   - description: `wave alpha tester`; task: leaf `tester`
   Wait for all results.

2. In one assistant response, make exactly three concurrent Agent calls:
   - description: `wave beta designer`; task: leaf `designer`
   - description: `wave beta reviewer`; task: leaf `reviewer`
   - description: `wave beta debugger`; task: leaf `debugger`
   Wait for all results.

3. Make one Agent call with description `coordinator`. Wait for its full branch. Build this
   exact five-layer chain:

   `coordinator` -> `planner` -> `docs-writer` -> `qa-reviewer` -> `release-tester`

   Each chain agent runs its commands first. Unless it is `release-tester`, it then makes one
   Agent call for next role with `subagent_type: "general-purpose"`, `model: "haiku"`, and
   role name as description, then waits for child. Include full tool restrictions, remaining
   chain, role commands, and these rules in each child task. `release-tester` is leaf.

Do not launch any other agents. Finish with `result: showcase complete`.
