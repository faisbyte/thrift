#!/usr/bin/env bash
# Launch the thrift MCP server (stdio). Installs dependencies on first run.
set -u
. "$(dirname "$0")/env.sh"
thrift_ensure_venv || exit 1
export THRIFT_PROJECT="${THRIFT_PROJECT:-$(thrift_project_dir)}"
exec "$THRIFT_PY" -m thriftctx.mcp_server
