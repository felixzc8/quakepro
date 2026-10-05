# QuakePro pi showcase

Run this exact, cheap, read-only showcase from project root. Start pi with:

```sh
pi --offline --tools read,bash,grep,find,ls @prompts/showcase-pi.md
```

## Hard rules

- Use only `read`, `bash`, `grep`, `find`, and `ls`.
- Do not write files or use network.
- Make no commentary between tool calls.
- Make each call once, in exact order below.
- `false` is expected to fail. Do not retry it.
- If any other call fails, stop and reply `result: showcase blocked: STEP failed`, replacing
  `STEP` with step number.

## Calls

1. Call `bash` with command `pwd`.
2. Call `read` with path `README.md`, offset `1`, and limit `5`.
3. Call `grep` with pattern `^class `, path `src/quakepro`, glob `*.py`, and limit `5`.
4. Call `find` with pattern `*.md`, path `.`, and limit `5`.
5. Call `ls` with path `.`, and limit `10`.
6. Call `bash` with command `false`. Treat its nonzero exit as expected, then continue.
7. Call `bash` with command `sleep 1`.

After all seven calls, reply exactly:

```text
result: showcase complete
```
