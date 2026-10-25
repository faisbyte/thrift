#!/usr/bin/env bash
# SessionStart hook: install deps if needed and refresh an existing index,
# fully detached so the session never waits on it.
. "$(dirname "$0")/env.sh" 2>/dev/null || exit 0
PROJECT="$(thrift_project_dir)"
(
    thrift_ensure_venv >/dev/null 2>&1 || exit 0
    if [ -f "$PROJECT/.thrift/index.db" ]; then
        "$THRIFT_PY" -m thriftctx.cli index --quiet --wait 0 "$PROJECT" >/dev/null 2>&1
    fi
) </dev/null >/dev/null 2>&1 &
exit 0
