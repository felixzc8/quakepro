# Contributing to QuakePro

Thanks for helping improve QuakePro.

## Before opening pull request

- Discuss larger changes in an issue first.
- Keep each pull request focused.
- Add or update tests for changed behavior.
- Run `uv run pytest`.
- Keep Python 3.10 support.
- Update docs when user-facing behavior changes.

## Development

```sh
uv sync --locked
uv run pytest
```

The installed `quakepro` command enters `src/quakepro/cli.py`; `./quakepro`
is a convenience launcher for a source checkout. Runtime modules live under
`src/quakepro/`, and tests live under `tests/`. See the [user guide's Files section](docs/guide.md#files)
for the module map.

Maintainers: see [the release procedure](docs/releasing.md).

QuakePro reads local Claude Code, Codex, and pi transcripts. Do not add real
transcripts, credentials, or private paths to tests, issues, or pull requests.

## Pull requests

Explain what changed, why, and how you tested it. Maintainers may request a
smaller change, tests, or documentation before merging.
