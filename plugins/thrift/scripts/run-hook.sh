#!/usr/bin/env bash
# UserPromptSubmit hook. Stdlib-only Python; never fails, never blocks.
. "$(dirname "$0")/env.sh" 2>/dev/null
PY="$THRIFT_PY"
[ -x "$PY" ] || PY="$(command -v python3 2>/dev/null)"
[ -n "$PY" ] || exit 0
"$PY" -S "$THRIFT_ROOT/scripts/prompt_hook.py" 2>/dev/null
exit 0
