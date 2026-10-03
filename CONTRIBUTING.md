# Contributing to QuakePro

Thanks for helping improve QuakePro.

## Before opening a pull request

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

## Contribution rules

- Work on a branch or fork and open a pull request against `main`.
- Describe the problem, resulting behavior, and validation. Link related issues.
- Use synthetic fixtures. Never include private session data or credentials.
- Keep discussions respectful and feedback focused on the work.
- Resolve review conversations before merging. New changes dismiss old approvals.

## Merge requirements

`main` requires a pull request, resolved review conversations, and passing CI on
Linux with Python 3.10 and 3.14 and macOS with Python 3.14. Branches must be up to
date with `main`. Force pushes and branch deletion are blocked, including for
administrators. Use squash or rebase merges to keep a linear history.

Contributor pull requests need one approving review. Repository administrators
may bypass the approval requirement through a pull request, allowing a sole
maintainer to merge their own work. This exception does not bypass CI, the
pull-request requirement, or resolved conversations.

The active rulesets are visible in [repository rules](https://github.com/felixzc8/quakepro/rules).
