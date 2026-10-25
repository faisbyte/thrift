"""dedup: replace a tool_result that is byte-identical to an earlier one with a reference.

Lossless as long as the referenced (first) occurrence stays visible in full: the
model can recover the output by looking at that earlier tool_use_id. The first
occurrence is never itself deduped, so references never chain.
"""

from __future__ import annotations

from .base import Result, Transform, register

MIN_CHARS = 200
_MAX_COMMAND = 80
_SUMMARY_KEY = {"Read": "file_path", "Bash": "command", "Grep": "pattern"}


def input_summary(tool_name: str | None, tool_input: dict) -> str:
    """Short, single-line description of what a tool call was asked to do."""
    key = _SUMMARY_KEY.get(tool_name or "")
    if not key:
        return ""
    value = " ".join(str(tool_input.get(key) or "").split())
    if tool_name == "Bash" and len(value) > _MAX_COMMAND:
        value = value[: _MAX_COMMAND - 1] + "…"
    return value


def text_only(block) -> bool:
    """True when the block's text is its whole content (no images or other parts to lose)."""
    content = block.raw.get("content") if isinstance(block.raw, dict) else None
    if not isinstance(content, list):
        return True
    return all(isinstance(c, dict) and c.get("type") == "text" for c in content)


def _reference(prev) -> str:
    summary = input_summary(prev.tool_name, prev.tool_input)
    ref = f"tool_use_id {prev.tool_use_id}" + (f", {summary}" if summary else "")
    return f"[reprouter: identical to the earlier {prev.tool_name} result ({ref}); output unchanged]"


class Dedup(Transform):
    name = "dedup"
    lossless = True

    def applicable(self, block, label, ctx):
        return (block.type == "tool_result" and not block.is_error
                and len(block.text) >= MIN_CHARS and text_only(block))

    def apply(self, block, label, ctx):
        first = next((prev for prev, _ in ctx.prior
                      if prev.type == "tool_result" and prev.text == block.text), None)
        if first is None:
            return None
        content = _reference(first)
        if len(content) >= len(block.text):
            return None
        return Result(content=content, lossless=True, note=f"dedup of {first.tool_use_id}")


register(Dedup())
