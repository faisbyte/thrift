# Shared environment for thrift launchers. Source this; don't execute it.
#
# Dependencies live in a venv keyed by the hash of uv.lock, under the
# plugin's persistent data dir; the code itself always runs from the plugin
# root via PYTHONPATH (so local edits take effect without reinstalling).

THRIFT_ROOT="${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)}"
THRIFT_DATA="${CLAUDE_PLUGIN_DATA:-${XDG_CACHE_HOME:-$HOME/.cache}/thriftctx}"

_thrift_lockhash() {
    if command -v shasum >/dev/null 2>&1; then
        shasum "$THRIFT_ROOT/uv.lock" | cut -c1-12
    else
        sha1sum "$THRIFT_ROOT/uv.lock" | cut -c1-12
    fi
}

THRIFT_VENV="$THRIFT_DATA/venv-$(_thrift_lockhash)"
THRIFT_PY="$THRIFT_VENV/bin/python"
export PYTHONPATH="$THRIFT_ROOT/src${PYTHONPATH:+:$PYTHONPATH}"

# Create the dependency venv if missing. Safe to call concurrently (uv locks).
thrift_ensure_venv() {
    [ -x "$THRIFT_PY" ] && [ -f "$THRIFT_VENV/.thrift-ok" ] && return 0
    if ! command -v uv >/dev/null 2>&1; then
        echo "thrift: 'uv' is required to install dependencies (https://docs.astral.sh/uv/)." >&2
        return 1
    fi
    mkdir -p "$THRIFT_DATA"
    UV_PROJECT_ENVIRONMENT="$THRIFT_VENV" uv sync --project "$THRIFT_ROOT" \
        --frozen --no-dev --no-install-project --quiet >&2 || return 1
    touch "$THRIFT_VENV/.thrift-ok"
    # drop venvs for older lockfiles
    for old in "$THRIFT_DATA"/venv-*; do
        [ "$old" != "$THRIFT_VENV" ] && [ -d "$old" ] && rm -rf "$old"
    done
    return 0
}

# Project the user is working in.
thrift_project_dir() {
    if [ -n "${CLAUDE_PROJECT_DIR:-}" ]; then echo "$CLAUDE_PROJECT_DIR"; else pwd; fi
}
