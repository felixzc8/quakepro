# Changelog

User-facing release notes for QuakePro.

## 0.1.0 — 2026-10-02

Initial alpha release.

### Highlights

- Monitor Claude Code, Codex, and pi sessions from local transcript files.
- View agent hierarchy in live Tree or one shared time scale in Timeline.
- See running, waiting, done, and failed states plus agent roles.
- Drill into recorded thoughts, messages, tool calls, outputs, and spawns.
- Show Claude workflows and teams in tabs, including shared team tasks.
- Group related Claude, Codex, and pi child agents into dispatch summaries.
- Watch several roots or Git worktrees in one run overview.
- Show recorded skill loads and orchestrate-workflow phases and events.
- Refresh from native file events and restore validated startup cache.
- Install and check user or project hooks with `setup` and `doctor`.
- Auto-open session panes in tmux or Herdr, with macOS terminal fallback.
- Navigate by arrow keys, Vim-style keys, mouse, or shortcuts page.

### Provider coverage

- Claude Code: Task/Agent sub-agents, workflows, agent teams, and team tasks.
- Codex: root rollouts, recursive child threads, and dispatch batches.
- pi: session trees plus foreground and live async sub-agents.
- Current pi-subagents saved child sessions include tool timelines and token
  usage, with native updates for related async status files.

### Safety and limits

- Monitoring sends no telemetry and makes no network requests.
- Monitoring is read-only. Hook setup edits client settings; Claude team
  messaging writes to selected teammate inbox.
- Transcript rendering neutralizes terminal control characters. Startup cache
  validates source identity and content before reuse.
- Claude teammate messaging rejects inbox path escapes.
- Cache locking rejects symlinks, hardlinks, and nonregular lock files before
  changing permissions.
- Claude API failures appear as errors and recover when a new turn begins.
- Default launch selects newest matching session; session picker is not built.
- Native watcher failure is shown and does not fall back to polling.
- Client transcript formats may change and require QuakePro update.

See [README.md](README.md) for install, provider details, privacy notes, and keys.
