#!/usr/bin/env bash
# CLI wrapper: thriftctx.sh index|search|symbol|related|stats ...
set -u
. "$(dirname "$0")/env.sh"
thrift_ensure_venv || exit 1
exec "$THRIFT_PY" -m thriftctx.cli "$@"
