"""UserPromptSubmit hook: inject a short "likely relevant" pointer block.

STDLIB ONLY and fail-silent: any problem (no index, locked/corrupt DB, slow
query, bad input) results in no output and exit code 0.

Env:
  THRIFT_HOOK=0          disable entirely
  THRIFT_HOOK_LOG=<file> append one JSON line per invocation (for benchmarks)
"""

from __future__ import annotations

import json
import os
import re
import sys
import time

DEADLINE_S = 0.25
MIN_COVERAGE = 0.3  # IDF-weighted coverage of the prompt terms
MAX_HITS = 6

_PASTED = re.compile(r"<pasted_content[^>]*>.*?</pasted_content[^>]*>", re.S)


def _log(entry: dict) -> None:
    path = os.environ.get("THRIFT_HOOK_LOG")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def build_context(payload: dict, t0: float | None = None, deadline: float = DEADLINE_S) -> str:
    """Return the additionalContext string, or "" to stay silent."""
    from . import store
    from .format import hook_context
    from .rank import BM25Ranker
    from .tokenize import parse_query

    t0 = t0 if t0 is not None else time.monotonic()
    prompt = payload.get("prompt") or ""
    prompt = _PASTED.sub(" ", prompt).strip()
    if not prompt or prompt.startswith("/"):
        return ""
    q = parse_query(prompt)
    # need some signal: >= 2 search terms, or an identifier-looking token
    if not q or (len(q.terms) < 2 and q.ident_like_count == 0):
        return ""

    start = os.environ.get("CLAUDE_PROJECT_DIR") or payload.get("cwd") or os.getcwd()
    root = store.find_project_root(start)
    if root is None and payload.get("cwd"):
        root = store.find_project_root(payload["cwd"])
    if root is None:
        return ""

    conn = store.open_readonly(root, busy_ms=50)
    try:
        abs_deadline = t0 + deadline
        # abort the SQL query if we run past the deadline
        conn.set_progress_handler(lambda: 1 if time.monotonic() > abs_deadline else 0, 2000)
        hits = BM25Ranker(conn).rank(q, MAX_HITS)
    finally:
        conn.close()
    if time.monotonic() - t0 > deadline or not hits:
        return ""
    hits = [h for h in hits if confident(h)]
    if not hits:
        return ""
    return hook_context(hits)


def confident(h) -> bool:
    """Only point at hits that match the prompt structurally.

    Conversational prompts ("thanks, looks good") tend to match prose in
    docstrings/comments only; real requests name things that appear in
    symbol names, paths or signatures.
    """
    if h.exact:
        return True
    return h.struct_matched >= 1 and len(h.matched) >= 2 and h.coverage >= MIN_COVERAGE


def main() -> None:
    t0 = time.monotonic()
    if os.environ.get("THRIFT_HOOK", "1") == "0":
        return
    ctx = ""
    err = None
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        ctx = build_context(payload, t0)
    except Exception as e:  # fail silent, by design
        err = f"{type(e).__name__}: {e}"
        ctx = ""
    _log({"ts": time.time(), "ms": round((time.monotonic() - t0) * 1000, 1), "chars": len(ctx),
          "fired": bool(ctx), "error": err})
    if ctx:
        sys.stdout.write(json.dumps({
            "hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": ctx}
        }))


if __name__ == "__main__":
    main()
