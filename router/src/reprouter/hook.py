"""PostToolUse hook adapter: compress a new tool result before Claude sees it.

Returns ``updatedToolOutput`` in the tool's own output shape (a mismatched
shape is silently ignored by Claude Code). Claude Code stores the replaced
output in its transcript, so caching, resume and preserved thinking stay
consistent without any replay. Bash only for now: its shape is
{stdout, stderr, interrupted, isImage, noOutputExpected}. Read is not
handled here because Claude Code adds line numbers after the hook runs.

Fails silent: any problem means no output and exit code 0.

Env: REPROUTER_MODE=off|passthrough disables; REPROUTER_KILL_FILE (default
~/.reprouter/off); REPROUTER_HOOK_LOG (decisions only, never content).
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from .classify import classify_block
from .request import Block
from .router import Policy, decide
from .transforms.base import Context

DEFAULT_KILL = os.path.expanduser("~/.reprouter/off")


def load_policy() -> Policy:
    from .config import build_policy

    return build_policy()


def handle_post_tool_use(payload: dict, policy: Policy, kill_file: str | os.PathLike = DEFAULT_KILL) -> dict | None:
    if Path(kill_file).exists() or payload.get("tool_name") != "Bash":
        return None
    resp = payload.get("tool_response")
    if not isinstance(resp, dict) or not isinstance(resp.get("stdout"), str) or resp.get("isImage"):
        return None
    block = Block(("hook", 0), "user", "tool_result", resp["stdout"], msg_index=0, tool_name="Bash",
                  tool_input=payload.get("tool_input") or {})
    root = payload.get("cwd")
    label = classify_block(block, project_root=root)
    decision, content = decide(block, label, Context(project_root=root), policy)
    if not isinstance(content, str):
        return None
    payload["_decision"] = decision
    return {"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                   "updatedToolOutput": {**resp, "stdout": content}}}


def main(argv: list[str] | None = None) -> int:
    try:
        if os.environ.get("REPROUTER_MODE", "on") in ("off", "passthrough"):
            return 0
        payload = json.loads(sys.stdin.read() or "{}")
        out = handle_post_tool_use(payload, load_policy(),
                                   kill_file=os.environ.get("REPROUTER_KILL_FILE", DEFAULT_KILL))
        if out is None:
            return 0
        log_path = os.environ.get("REPROUTER_HOOK_LOG")
        d = payload.get("_decision")
        if log_path and d is not None:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": time.time(), "tool": payload.get("tool_name"), "kind": d.kind,
                                    "transform": d.transform, "before": d.tokens_before,
                                    "after": d.tokens_after}) + "\n")
        sys.stdout.write(json.dumps(out))
    except Exception:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
