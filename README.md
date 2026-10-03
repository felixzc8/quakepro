# QuakePro

Watch Claude Code, Codex, and pi agents in one live terminal interface. Follow
parallel work, nested agents, and failures, then inspect the recorded actions.
QuakePro reads local session files; it needs no API connection or client patch.

![Tree view with two groups of parallel agents and a five-level nested chain; checkmarks, arrows, and an error mark distinguish completed, running, and failed work.](https://raw.githubusercontent.com/felixzc8/quakepro/main/docs/assets/quakepro-tree.png)

*Actual QuakePro UI with synthetic session data. Two agent waves and a nested
release workflow demonstrate roles, phases, and mixed statuses. No private transcripts.*

## Quick start

Requires Python 3.10+, Git, and a Unicode-capable terminal. Install
[uv](https://docs.astral.sh/uv/getting-started/installation/) first if needed.

The [0.1.0 alpha](https://github.com/felixzc8/quakepro/releases/tag/v0.1.0) is
available on GitHub; PyPI publication is pending.

```sh
uv tool install 'git+https://github.com/felixzc8/quakepro.git@v0.1.0'
quakepro
```

Run it beside a coding client that has created a session. QuakePro opens the
newest matching session. If the command is missing, run `uv tool update-shell`
and reopen your terminal. With pipx, install using the same Git URL.

To automatically open a side pane when a subagent starts, run `quakepro setup`.
This optional step updates local client hook settings.

## Choose what to watch

```sh
quakepro --provider codex        # one client: claude, codex, or pi
quakepro --project myproject     # match a project path
quakepro --file /path/run.jsonl  # a specific transcript
quakepro --worktrees             # sessions across this repo's worktrees
```

Use repeated `--root DIR` options to watch several projects together.

## See overlap and inspect failures

Press **v** to switch to Timeline. Each agent gets a lane on the same time scale,
so concurrent work and the later nested chain are easy to compare.

![Timeline of eleven agents: parallel agents in two waves, a later nested chain, and a debugger lane ending with a failure mark.](https://raw.githubusercontent.com/felixzc8/quakepro/main/docs/assets/quakepro-timeline.png)

Press **Enter** on an agent, then expand an action to read its input and output.
Here, the mock debugger's deliberately failing command is open for inspection.

![Failure inspector showing the selected debugger, the false command, exit code 1, and its command output beneath the agent tree.](https://raw.githubusercontent.com/felixzc8/quakepro/main/docs/assets/quakepro-inspector.png)

Screenshots use the same synthetic workflow and the real app renderer. Status
symbols and text accompany colors. [Reproduce the captures](https://github.com/felixzc8/quakepro/blob/main/docs/guide.md#reproduce-the-screenshots).

## Essential keys

| Key | Action |
|---|---|
| Arrow keys or `h j k l` | Move through the tree; expand or collapse branches |
| `Enter` | Inspect an agent or expand an action |
| `v` | Switch Tree / Timeline |
| `?` | Show all shortcuts |
| `q` | Quit |

## Compatibility and privacy

- Supports local Claude Code, Codex, and pi transcripts. Client format changes
  may require an update. See [tested versions and limits](https://github.com/felixzc8/quakepro/blob/main/docs/release-validation.md).
- Auto-open supports tmux and Herdr. Without either, launch QuakePro manually.
- There is no session picker yet. Use the options above to select a session.
- Monitoring makes no network requests and leaves transcripts unchanged.
  Transcripts and the local cache may contain secrets already recorded by a client.
- Hook setup edits client configuration. Optional Claude teammate messaging
  writes to the selected teammate's inbox.

## Learn more

[User guide](https://github.com/felixzc8/quakepro/blob/main/docs/guide.md) ·
[Setup and removal](https://github.com/felixzc8/quakepro/blob/main/docs/guide.md#auto-open-hooks) ·
[Contributing](https://github.com/felixzc8/quakepro/blob/main/CONTRIBUTING.md) ·
[Changelog](https://github.com/felixzc8/quakepro/blob/main/CHANGELOG.md) ·
[Report a security issue](https://github.com/felixzc8/quakepro/security/advisories/new)

Licensed under [Apache 2.0](https://github.com/felixzc8/quakepro/blob/main/LICENSE).
