#!/bin/sh
DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P) || exit 0
[ -e "$DIR/DISABLED" ] && exit 0
PY="$DIR/.venv/bin/python"
[ -x "$PY" ] && [ -r "$DIR/src/quakepro/pane_lifecycle.py" ] || exit 0
PYTHONPATH="$DIR/src${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONPATH

exec "$PY" -m quakepro.pane_lifecycle codex "$@" 2>/dev/null
