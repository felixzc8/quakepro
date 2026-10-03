# QuakePro user guide

For installation and a quick overview, see the [README](../README.md).
PyPI publication is pending; use its GitHub install command in place of the
package-name examples below until publication.

- [Skills and workflow phases](#skill-view)
- [Provider support](#provider-support)
- [Multiple projects and worktrees](#per-run-overview)
- [Privacy and local data](#privacy-and-local-data)
- [Hook setup, upgrades, and removal](#auto-open-hooks)
- [All keyboard shortcuts](#keys)
- [Source layout](#files)

## Reproduce the screenshots

The Codex split-pane images show a real read-only review of the public repository,
including parallel parser and UI reviewers and a nested accessibility reviewer. They render the
actual ANSI contents of two side-by-side tmux panes, rather than an operating
system window screenshot. No transcript text or agent states are invented.

To capture your own two-pane window, review both panes for private information,
then run:

```sh
uv run --with resvg-py scripts/capture_split_pane.py \
  --socket quakepro-readme --target review:0 \
  --output docs/assets/quakepro-codex-tree.png
```

The socket and target identify your tmux session. Press `v` in the QuakePro pane
and repeat with a different output filename to capture Timeline.

The standalone Tree, Timeline, and failure-inspector images use synthetic session
records based on `prompts/showcase-claude.md` and `prompts/showcase-codex.md`.
The simulated failure is intentional. Reproduce those examples with:

```sh
uv run --with resvg-py scripts/capture_showcase.py
```

The script captures Tree, Timeline, and the failure inspector into `docs/assets/`.
It runs the normal UI in Textual's test driver, exports its screen, and rasterizes
it with resvg. It launches no agents and reads no private sessions or client
settings. Rendering uses installed fonts; the checked-in captures use Menlo on
macOS.

## Skill view

Press `s` in graph view to toggle skill details (hidden by default). When on, each
agent that loaded a skill carries `⟡ <skill>` with its latest recorded skill name.
Display stays transcript-derived; QuakePro reads no skill files itself. Drill-in
panel shows underlying call.

The recognizers cover each client's own shapes: Claude Code's `Skill` tool call, a `read`
of a skill definition (`<skill>/SKILL.md`, or a bare `.md` directly under a `skills/`
directory) since pi has no skill tool, and a shell command that reads a `SKILL.md` path out
(`cat`, `sed`, `head`, and friends) since Codex reads files by running one — grepping,
deleting, or writing that file is authoring the skill, not loading it.

## Orchestrate-workflow awareness

Agent nodes also carry the orchestrate workflow they are running, always visible — no
overlay key:

- `▷ <phase>` — `spec`, `tickets`, `implement`, `review`, or `orchestrating`, from the
  agent's latest skill load — `to-spec`, `to-tickets`, `tdd`, `implement`, `code-review`,
  `review-falsification`, or `orchestrate` — or
  its latest review dispatch. Ticket and board work happens in every phase, so it names
  none.
- `⊞ <event>` — its latest ticket, board, or review event: a ticket file written under
  `.scratch/<feature>/issues/`, that file's `**Status:**` line moved to a new value (shown
  with it), a `blackboard show|read|open|stop|list` command, or a subagent launched to run
  the falsification review.

All three providers feed the same recognizers, so a run overview reads every agent's phase
at a glance while the drill-in panel still shows the calls behind it. Codex encrypts the
message it sends a spawned agent, so its spawns surface as `agent dispatched` — a dispatch
happened, with no review verdict on it.

## Subagent roles

Each subagent carries one role icon after its lifecycle icon. Tree rows and drill
headers show individual roles. Tree role legend folds at frame edge. Timeline rows
show ordered role icons when width permits. Drill headers also name role.

| Icon | Role | Purpose |
|---|---|---|
| `⌕` | research | Inspect code and gather facts |
| `◇` | design | Plan, specify, or design |
| `⌨` | code | Build, refactor, or fix code |
| `⚗` | tests | Write or run tests, builds, or checks |
| `◎` | review | Assess code, specs, or records |
| `⚠` | debug | Reproduce failure and find cause |
| `≡` | docs | Write documentation or records |
| `◆` | coordinate | Own, dispatch, or combine work |
| `·` | other | Recorded evidence does not name one role |

Classification matches visible agent name against ordered role keywords. First match
wins; no match stays `other`. Prompts, skills, actions, and workflow phase never relabel
agent. Fix and repair names use code icon and style.

## Provider support

| Provider | Data source | Displayed structure |
|---|---|---|
| Claude Code | `~/.claude/projects/` or `$CLAUDE_CONFIG_DIR/projects/` | Main loop, Task/Agent sub-agents, workflows, agent teams, and shared team tasks |
| Codex | `~/.codex/sessions/` or `$CODEX_HOME/sessions/` | Main thread and recursively spawned sub-agents |
| pi | `~/.pi/agent/sessions/` (`$PI_CODING_AGENT_DIR`, or `$PI_CODING_AGENT_SESSION_DIR` for the whole location) | One session node plus foreground and live async sub-agents |

The `session` tab contains the main loop and its sub-agents for any provider.
Claude workflow runs and agent teams get additional tabs. Codex and pi sessions use
one session tab because their work forms a single tree.

Press `v` to switch current tab between `Tree` and `Timeline`; current
view is named in header. Tree stays exact agent hierarchy with its selection,
collapsed branches, and drill state intact.

Timeline gives each recorded agent run its own lane. It does not infer or merge work
from ticket text, goals, prompts, parent links, review, or fix terms. Structural root,
team, task, and dispatch nodes stay out. Nested agents get separate lanes. Each lane
keeps one palette color; failed agents stay red. Live transcript updates redraw chosen
view without resetting Tree state.

Timeline puts all agents on one recorded session-time scale. Aligned lanes and marks
distinguish roles, parallel runs, active spans, failures, and missing timestamps.
Its compact cue help lists only marks present in session.
Medium and narrow panes keep every lane and its marks, including panes below twenty
cells.

Claude Code sub-agents write the same pair of files under
`~/.claude/projects/<encoded-cwd>/<sessionId>/subagents/`:

```
agent-<id>.jsonl        the agent's transcript (shared shape across all kinds)
agent-<id>.meta.json    what kind of agent it is
```

and the `meta.json` discriminates the kind:

- **Simple Task/Agent sub-agent** — `agentType` (e.g. `general-purpose`) +
  `toolUseId` + `spawnDepth`. QuakePro keys the node by `agentId` and wires its
  parent via `toolUseId` → the spawning `tool_use` in the main transcript. (The
  main transcript's `progress`/`agent_progress` events are still read as a faster
  live overlay onto the same node — disk is the source of truth, the stream just
  fills in sooner.)
- **Workflow agent** — `agentType:"workflow-subagent"`, stored under
  `subagents/workflows/<runId>/` with a `journal.jsonl` (`started`/`result`
  lifecycle). The human run name comes from the `Workflow` tool_use script's
  `meta.name`, linked to the `runId` via the tool_result.
- **Team teammate** — `taskKind:"in_process_teammate"` + `teamName` + `name` +
  `model`. QuakePro groups teammates by `teamName` under a team node, and reads
  the live roster from `~/.claude/teams/<teamName>/config.json` — a teammate is
  shown running while it is in `members`, and done once it is pruned from the
  roster.

Codex writes one rollout JSONL file per thread under dated directories. QuakePro
uses each rollout's `session_meta` to connect child threads to their parent and
tails newly discovered descendants while the session runs. Codex may encrypt
delegated task bodies and reasoning in its local transcript; QuakePro shows the
available agent path, messages, calls, results, model, and token counts without
attempting to decrypt protected fields.

Codex same-parent `spawn_agent` calls form one dispatch while only spawn results and
child activity notices occur between them. Parent reasoning, text, waiting, or another
tool call starts a later dispatch. Claude groups Task and Agent calls from one parent
assistant message. A singleton stays a direct child. A multi-agent batch gets a `◆`
summary whose leading label comes only from recorded child goals: Codex `task_name`,
Claude `description` (or its specific agent type), and pi child-result `task`. Shared
goal prefixes compress; unrelated, generic, or absent goals omit the leading label.
Agent and state counts remain visible, then agents stay in
launch order even when start notices arrive later or reversed. Nested batches stay beneath
spawning agent. Batches with four or more
completed agents start collapsed; active or failed batches stay open. `Enter` toggles a
batch row, and manual branch choices survive later transcript updates.

pi keeps one append-only JSONL file per session in a directory named after the
working directory (`--<cwd with separators as dashes>--`). The first line is a
`session` header carrying `cwd` and the session id; every later line is a tree entry
with `id` and `parentId`. QuakePro renders those entries in the order pi wrote them,
so a `/tree` branch reads as later actions rather than a separate view. A pi turn shows
running while it stopped to use a tool, waiting once it completed, and errored when it
aborted or failed — pi writes no separate lifecycle records.

pi core ships no sub-agent tool. Current `pi-subagents` releases return an async run
directory in parent tool result. QuakePro reads its bounded `status.json` as live
topology and state, watches `events.jsonl`, and tails validated child `sessionFile`
transcripts. One run with several children renders as one dispatch summary; a
singleton renders directly as child node. `subagent_wait` completion data reconciles
terminal state when live artifacts are already gone. Older foreground results with
embedded `details.results[]` remain supported.

> The pi reader was written from pi's own shipped schema
> (`docs/session-format.md` and `dist/core/` in `@mariozechner/pi-coding-agent`) and is
> exercised by schema-derived fixtures in `tests/pi_fixture.py`. Core messages and
> built-in tools and foreground/async children were also tested with live pi 0.87.1
> and pi-subagents 0.73.1 sessions. Async artifact
> support follows `pi-subagents` observability contract; unknown fields and events
> are ignored.

## Per-run overview

One QuakePro can watch a whole agent run instead of one directory: point it at the
run's roots and it draws every session it finds under each of them in one tree,
grouped by root (`⌂`).

```sh
quakepro --root ~/work/repo --root ~/work/repo-wt-2   # explicit roots
quakepro --worktrees                                  # every worktree of this repo
quakepro --root ~/work/repo --worktrees               # both, explicit roots first
```

Each root contributes the newest session of each provider that ran there, so the view
stays on the live agents rather than the directory's history. Sessions that start after
QuakePro opened attach on their own. The overview has one tab, so workflow and team
nodes expand in place instead of getting tabs of their own, and it offers no write
action.

## Startup cache

QuakePro keeps a versioned, derived session snapshot under
`$TMPDIR/quakepro` (or the platform temporary directory). Set
`QUAKEPRO_CACHE_DIR` to move it. Each selected root has its own cache file.

Before restore, QuakePro checks exact identities and content hashes for every
parsed Claude, Codex, and pi source. Watched directories keep name and file-kind
manifests so added, removed, or unsafe files reject only affected root cache.
Unchanged prefixes restore parsed nodes and transcript offsets; appended complete
records then pass through normal live parsers. Partial final records wait for more
bytes.

pi snapshots retain async run identity and child transcript deduplication state.
Available artifacts reconnect after restore; terminal tree stays visible after temp
artifact cleanup.

Cache writes use a temporary file and atomic replace. Missing, corrupt,
incompatible, malformed, or unwritable cache falls back to current source files.
Cache contains no rendered layout, selection, expanded rows, or view choice.
Deleting cache is always safe.

## Privacy and local data

QuakePro sends no telemetry and makes no network requests. It reads local
provider transcripts, team metadata, and task files needed for the selected
view. Rendered thoughts, messages, tool inputs, and tool outputs can contain
secrets already present in those files.

Startup cache contains transcript-derived nodes and actions. The `session-state-v3`
subdirectory is owner-only and cache files are mode `0600`. Delete `$TMPDIR/quakepro`, the
platform temporary-directory equivalent, or `QUAKEPRO_CACHE_DIR` to clear it.

Monitoring does not change provider transcripts. `quakepro setup` updates local
provider hook configuration. On Claude team tabs, the optional `i` action writes
the message to selected teammate's local inbox.

QuakePro never talks to whatever launched a manual run. To attach one to a run
overview, open a pane and point it at that run's roots:

```sh
tmux split-window -h -p 40 'cd ~/work/repo && quakepro --worktrees'
```

## Install

QuakePro needs Python 3.10+. Recommended install uses
[uv](https://docs.astral.sh/uv/):

```sh
uv tool install quakepro
quakepro setup
```

`setup` finds Claude Code, Codex, and pi commands on `PATH`, installs hooks for
each, then checks them. It preserves unrelated settings. Run
`uv tool update-shell` and reopen terminal if `quakepro` command is missing.

Fallback install:

```sh
pipx install quakepro
quakepro setup
```

For source work:

```sh
git clone https://github.com/felixzc8/quakepro.git
cd quakepro
uv sync
./quakepro
```

Run tests with `uv run pytest`.

## Run

```sh
quakepro                                  # newest Claude, Codex, or pi root session
quakepro --provider codex                 # newest Codex session
quakepro --provider claude                # newest Claude Code session
quakepro --provider pi                    # newest pi session
quakepro --project myproject              # filter by project path substring
quakepro --file <path>.jsonl              # detect provider from explicit file
quakepro --provider codex --file <path>.jsonl
quakepro --root DIR [--root DIR] [--worktrees]   # per-run overview
```

For Codex, an explicit file may be either the root rollout or a child rollout;
QuakePro resolves the session root. Provider overrides are validated, so a Codex
file cannot accidentally be parsed as Claude Code or vice versa.

## Auto-open hooks

QuakePro installs no daemon. TUI watches transcript files only while open.
Provider hooks run short `quakepro _hook` commands on lifecycle events. First
Claude or Codex `SubagentStart`, or pi subagent launch, opens one pane for that
session. Root session startup alone opens nothing. `SessionEnd` closes opened pane.

Common setup:

```sh
quakepro setup                         # clients found on PATH
quakepro setup --provider all          # Claude Code, Codex, and pi
quakepro setup --provider claude       # one client
quakepro doctor --provider all         # read-only check
```

User hooks live in `~/.claude/settings.json`, `~/.codex/hooks.json`, and
`~/.pi/agent/extensions/quakepro.js`, or homes set by `CLAUDE_CONFIG_DIR`,
`CODEX_HOME`, and `PI_CODING_AGENT_DIR`. Project hooks use current Git root:

```sh
quakepro setup --provider all --scope project
quakepro setup --provider codex --scope project --root /path/to/repository
```

Setup edits provider config atomically, keeps unrelated hooks, and is safe to
repeat. Hook commands point at installed `quakepro` executable, so they survive
tool upgrades. Setup prints restart and trust steps. Codex users must open `/hooks`
and trust exact command. Project hooks and pi extensions need project trust.

All three clients share pane rules. Each session gets its own 40% right pane in
tmux or Herdr. Hook finds nearest host from process ancestry, keeps focus in work
pane, and turns tmux mouse support on. Without either host, no window opens;
launch QuakePro manually. Concurrent starts are deduplicated.

Claude and Codex lifecycle events feed secure local state. `Stop` marks main loop
waiting, `SubagentStop` records terminal agent state, and `SessionEnd` closes
matching pane. pi state still comes from its transcript; its extension sends only
subagent-triggered session start and matching end. Async launch uses pi-subagents'
public `subagent:async-started` event. Foreground launch uses pi's public
`tool_call` event because pi-subagents exposes no foreground-start event. Hooks
never change transcript or steer provider.

### Upgrade

```sh
uv tool upgrade quakepro
quakepro setup
```

Setup refreshes hook details and runs checks. With pipx, use `pipx upgrade quakepro`.

### Remove

Remove hooks before command:

```sh
quakepro hooks uninstall --provider claude
quakepro hooks uninstall --provider codex
quakepro hooks uninstall --provider pi
uv tool uninstall quakepro
```

Add `--scope project` and optional `--root` to remove project hooks. With pipx,
last command is `pipx uninstall quakepro`.

### Manual tmux pane

```sh
tmux new-session \; send-keys 'claude' C-m \; \
  split-window -h -p 40 'quakepro' \; \
  select-pane -L
```

For Codex, replace `claude` with `codex` and add `--provider codex` to the
QuakePro command. From an existing tmux session, press `Ctrl-b %` and run
`quakepro` in the new pane.

An agent's own work — every thought, spoken turn, tool call, and spawn — is part
of the same graph. Press `⏎` on an agent to open its action timeline: each action
becomes a child node titled by its type (`▸` tool, `▪` text, `✶` thinking, `◆`
spawn). A tool shows stable `◍` while in flight and `✗` if it errored. Spawn
node identifies the sub-agent it launched. Inside the timeline, press `⏎` on an
action to inspect it — a thought or text shows its full content, and a tool shows
its input and output.

## Keys

- `v` switch between `Tree` and `Timeline`
- `shift+←` `shift+→` switch between tabs while navigating tabs, the tree,
  or the inspector (message input uses these keys for text selection)
- `↑` `↓` move through visible Tree nodes; `shift+↑`
  `shift+↓` jump to previous or next Tree node at same depth
- `h` `j` `k` `l` mirror arrow navigation; `gg` `G` jump to first or last item
- `ctrl+u` `ctrl+d` move half a page; `ctrl+b` `ctrl+f` move a full page
- `gT` `gt` switch to previous or next tab; `[[` `]]` move at same depth
- `zo` `zc` `za` open, close, or toggle one branch; `zR` `zM` open or close all
- `zt` `zz` `zb` place selection at top, middle, or bottom of view
- `a` jump to the next visible running or errored agent, wrapping at the end
- `[a` `]a` jump to previous or next running or errored agent
- `c` collapse every branch in current tab; press again to expand them
- `s` show or hide latest skill on each agent
- `→` expand the selected node's child agents; `←` collapse them
- `⏎` open selected Tree agent's action timeline; inside it, `⏎` or `→`
  reveals selected instruction, action, tool input, or tool output.
- `i` message the selected teammate (team tabs only)
- `?` open shortcuts page; `?`, `esc`, or `q` closes it
- `esc` cancel a message draft, close the inspector, or leave the graph
- `q` quit

The UI uses Textual's `ansi-dark` theme, so it inherits your terminal's colors
and background rather than imposing its own.

## Files

- `src/quakepro/claude_model.py` — tails Claude transcripts and workflow stores into node tree
- `src/quakepro/codex_model.py` — tails Codex root and child rollouts into same node model
- `src/quakepro/pi_model.py` — tails pi session into same node model, plus pi session paths
- `src/quakepro/overview.py` — run roots (explicit and git worktrees) and combined per-run tree
- `src/quakepro/skill_mode.py` — recognizes skill loads in tool calls
- `src/quakepro/workflow.py` — recognizes orchestrate-workflow tickets, boards, reviews, and phase
- `src/quakepro/shell.py` — reads command line as commands it runs, for those recognizers
- `src/quakepro/session_identity.py` — provider detection and Codex root/child identity
- `src/quakepro/sessions.py` — newest-session discovery and model selection
- `src/quakepro/observation.py` — native file events, burst reconciliation, watcher recovery
- `src/quakepro/lifecycle.py` — secure hook journal and ordered lifecycle state
- `src/quakepro/graph.py` — lays out Tree and Timeline projections
- `src/quakepro/app.py` — Textual tabs, views, navigation, and exact drill
- `src/quakepro/cli.py` — installed command and subcommand routing
- `src/quakepro/pane_lifecycle.py` — shared pane open, wake, dedupe, and close logic
- `src/quakepro/hook_config.py` — setup and doctor across providers
- `src/quakepro/claude_hooks.py`, `codex_hooks.py`, `pi_hooks.py` — safe provider config edits
- `open-pane.sh`, `close-pane.sh`, `codex-open-pane.sh` — source-checkout hook adapters
- `quakepro` — source launcher (uses `.venv`)

## Contributing

See [CHANGELOG.md](https://github.com/felixzc8/quakepro/blob/main/CHANGELOG.md)
for release notes and
[CONTRIBUTING.md](https://github.com/felixzc8/quakepro/blob/main/CONTRIBUTING.md)
for development steps. Report security issues through private process in
[SECURITY.md](https://github.com/felixzc8/quakepro/blob/main/SECURITY.md).

## License

QuakePro is licensed under
[Apache License 2.0](https://github.com/felixzc8/quakepro/blob/main/LICENSE).

## Next steps (not yet built)

- Overlay the task DAG from `~/.claude/tasks/session-*/` (`blocks`/`blockedBy`)
- Render `SendMessage` traffic between teammates (from `teams/*/inboxes/`) as edges
- Session picker when several are live
