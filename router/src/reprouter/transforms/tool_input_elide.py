"""Elide large bodies from the model's own past tool calls.

Write/Edit bodies and Bash heredoc scripts are generated once by the model,
then resent (and re-read from cache) on every later turn. In the recorded
corpus they are the single largest share of cache-weighted input cost. Once a
call has run, the full body rarely matters: the file on disk has the result,
and a script's output follows in its tool_result. This keeps the first and
last lines of each large string field, plus a stub saying how to recover it.

Delivered only by the proxy, deterministically from the request prefix. The
edited bytes sit after the thinking block in their own message, and every
later turn sees the same elided bytes.
"""

from __future__ import annotations

from .base import Context, Result, Transform, register

MIN_CHARS = 800
HEAD_LINES = 3
TAIL_LINES = 2
TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit", "Bash"}
FIELDS = {"content", "old_string", "new_string", "new_source", "command"}


def _elide(value: str, hint: str) -> str:
    lines = value.split("\n")
    if len(value) <= MIN_CHARS or len(lines) <= HEAD_LINES + TAIL_LINES + 2:
        return value
    hidden = len(lines) - HEAD_LINES - TAIL_LINES
    stub = f"[reprouter: {hidden} more lines ({len(value):,} chars) elided after the call ran; {hint}]"
    return "\n".join(lines[:HEAD_LINES] + [stub] + lines[-TAIL_LINES:])


class ToolInputElide(Transform):
    name = "tool_input_elide"
    lossless = False

    def applicable(self, block, label, ctx: Context) -> bool:
        return block.type == "tool_use" and block.tool_name in TOOLS and isinstance(block.tool_input, dict)

    def apply(self, block, label, ctx: Context) -> Result | None:
        hint = ("the command already ran and its output follows" if block.tool_name == "Bash"
                else "the file on disk has the result; Read it if you need the exact text")

        def walk(obj):
            if isinstance(obj, dict):
                return {k: (_elide(v, hint) if k in FIELDS and isinstance(v, str) else walk(v)) for k, v in obj.items()}
            if isinstance(obj, list):
                return [walk(x) for x in obj]
            return obj

        new = walk(block.tool_input)
        if new == block.tool_input:
            return None
        return Result(content=new, lossless=False)


register(ToolInputElide())
