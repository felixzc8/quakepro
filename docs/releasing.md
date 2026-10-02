# Releasing QuakePro

Releases use `uv` and the tag-triggered `.github/workflows/release.yml` workflow.

## Before tagging

1. Update the version with `uv version <version>` and add dated release notes to
   `CHANGELOG.md`. Review the runtime dependencies and supported Python versions.
2. Run `uv sync --locked --all-groups`, then `uv run --no-sync pytest`.
3. Build into a fresh directory and check both distributions outside the project:

   ```sh
   release_dist=$(mktemp -d)
   uv build --no-sources --out-dir "$release_dist"
   uv run --isolated --no-project --with "$release_dist"/*.whl quakepro --version
   uv run --isolated --no-project --with "$release_dist"/*.whl quakepro --help
   uv run --isolated --no-project --with "$release_dist"/*.tar.gz quakepro --help
   ```

4. Test the installed package with real Claude Code, Codex, and pi sessions.
   Check child-agent discovery, live updates, Tree/Timeline navigation, and
   drill-in. In disposable settings and terminal sessions, check setup, doctor,
   pane open/close, repeated setup, and hook removal. Record client versions and
   distinguish real client events from synthetic fixtures. Keep private
   transcripts out of release artifacts and reports.
5. Confirm CI passes on the exact release commit, including Linux/Python 3.10
   and 3.14 and macOS/Python 3.14. Check that the README describes any remaining
   compatibility limits.
6. Verify PyPI has a trusted publisher for owner `felixzc8`, repository
   `quakepro`, workflow `release.yml`, and environment `pypi`. For the first
   release, configure a pending publisher with project name `quakepro`.
   Verify the GitHub `pypi` environment permits release tags and private
   vulnerability reporting is enabled for the public repository.

## Publish

Push the reviewed commit, then create and push its matching version tag:

```sh
git tag -a "v$(uv version --short)" -m "QuakePro $(uv version --short)"
git push origin "v$(uv version --short)"
```

The workflow checks the tag against package metadata, tests, builds, checks
isolated installs, and generates SHA-256 checksums. It publishes to PyPI using
short-lived trusted-publisher credentials, then creates the GitHub release with
the wheel, source distribution, and checksums.

Verify the workflow succeeds and the release assets match its checksums. In a
fresh tool directory, run `uv tool install quakepro==<version>` and check
`quakepro --version`, `quakepro --help`, and a real session. Check README links
and images on both GitHub and PyPI.

## Recover a failed release

Inspect the failed job before retrying. If only publisher configuration or
GitHub release creation failed, fix that configuration and rerun failed jobs
with the original build artifacts. `uv publish` skips identical files already
uploaded to PyPI. Do not rebuild different contents under an uploaded version
or move a published release tag.

If the package itself needs a fix after upload, release a new patch version.
For a broken published version, assess whether to yank it on PyPI and explain
the replacement in the release notes. Preserve the previous artifacts for
diagnosis.
