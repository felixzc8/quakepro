# QuakePro 0.1.0 release validation

Acceptance testing on 2026-10-02 used macOS, Python 3.12.4, uv 0.12.5,
Textual 8.2.7 (locked environment) and 8.2.8 (isolated installs), and watchfiles
1.2.0 (locked) and 1.3.0 (isolated installs). Client tests used temporary projects and
isolated hook settings. No private transcripts are included in this repository.

## Codex

Codex CLI 0.145.0 with `gpt-5.6-sol` produced real parallel children and a nested
grandchild. QuakePro displayed the correct hierarchy, dispatch grouping,
completion states, model names, and token counts. Selecting the grandchild's
rollout resolved the full root tree. Tree, Timeline, drill-in, and run overview
worked.

With the UI attached before work began, native file events displayed the child
and its actions, then completion, without an observation error. Parsing and
overview left completed transcript hashes unchanged.

User/project setup, doctor, repeated setup, and uninstall preserved unrelated
configuration. A dedicated tmux server received actual SessionStart,
UserPromptSubmit, SubagentStart, SubagentStop, Stop, and SessionEnd hooks. Root
startup opened no pane; the first child opened one approximately 40% right pane
without taking focus; SessionEnd closed it. Mouse support was enabled.

The isolated test used Codex's hook-trust bypass for its vetted hook commands.
Interactive `/hooks` trust and macOS terminal fallback were not exercised.
This CLI rejected `gpt-6-astra` as requiring a newer client; successful tests used
the compatible model above.

## Claude Code

Claude Code 2.1.259 produced an actual authentication-failure transcript and
successful SessionStart, UserPromptSubmit, and SessionEnd hooks. Installed-tool
setup, doctor, repeated setup, and removal preserved unrelated settings and
hooks. The real failure transcript rendered in the inspector and Tree/Timeline
without changing its bytes. Replaying that captured transcript through native
file events updated the error status while preserving selection and view.

Acceptance uncovered and fixed API failures remaining marked running, including
when older hook state was replayed. A fresh prompt clears the error state.
Manually injected lifecycle payloads in a real tmux server verified the 40%
pane, retained focus, duplicate-start suppression, mouse setting, and closure.
These injected SubagentStart events are not a live Claude subagent test.

Successful model calls and real subagent creation remain unverified in this
acceptance run: both retries and minimal safe mode failed with an expired OAuth
session that could not be refreshed. This is an external authentication limit.

## pi

pi 0.87.1 with pi-subagents 0.73.1 and `vercel-ai-gateway/openai/gpt-6-sol` ran
real core-tool, foreground-child, and asynchronous-child sessions. Core tests covered read, bash, write, edit, an
expected failing command, skill recognition, tool-result pairing, model/token
metadata, and the final waiting state.

Acceptance found that current foreground results reference a saved session
instead of embedding child messages. It also found that current asynchronous
status records use a run identifier distinct from the child transcript directory,
and macOS temporary paths may use `/var` and `/private/var` aliases. The fixes
read validated child sessions, verify status relationships, and watch sibling
status dependencies while preserving path confinement.

After the fixes, a fresh foreground child displayed its read result, final text,
and token usage. In a fresh interactive asynchronous run, two children attached
through native events while running, displayed their active tools, then completed
with their full recorded steps and tokens. Timeline worked. The generated pi
extension emitted matching SessionStart and SessionEnd events. User/project
setup, repeated setup, doctor, and removal passed in isolated configuration.
An actual RPC abort during a running bash tool produced error states. Real
branch/compaction behavior and nested asynchronous descendants were not exercised;
pi's hook payloads went to a safe recorder, with pane behavior tested separately.

## Installed application and local data

Both the wheel and source distribution built with `uv build --no-sources` and
passed isolated installed-command smoke tests. The installed application ran in
a real terminal and through Textual's test driver with synthetic transcripts.
Native append events refreshed the display; partial records waited for their
completion; warm cache restored and caught up. Tree/Timeline switching,
drill-in, help, narrow terminal resizing, and quit worked. Source contents,
permissions, inode, and modification time remained unchanged by monitoring.

A synthetic OSC 52 terminal-control payload remained inert. The source and
distribution review found no high-confidence credential or private-key matches,
and no transcript, environment, Git, scratch, or virtual-environment files in the
distribution archives. This was a scoped release review, not a penetration test.
The no-network claim was checked by source inspection; runtime network traffic
was not instrumented.

The review reproduced and fixed cache-lock symlink permission changes, cache-base
symlink redirection, and teammate-inbox writes through symlinked directories.
Cache admission now rejects unsafe bases and lock files. Inbox writes use pinned
directory descriptors and reject unsafe files; regressions cover directory
replacement, symlinks, hardlinks, concurrent writers, and recovery behavior.

## Coverage limits

Live provider and pane acceptance ran on macOS. Linux coverage comes from CI's
test suite, including the real native-file-event integration test. Herdr,
iTerm2/Terminal fallback, and every client/version combination were not tested.
Provider transcript formats can change independently of QuakePro.
