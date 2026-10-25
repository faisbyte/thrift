"""Lossless presentation cleanup for command-style tool results."""

from __future__ import annotations

import re

from .base import Context, Result, Transform, register

_ANSI = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]"  # CSI: colors, cursor movement, erase
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC: titles, hyperlinks (BEL or ST terminated)
)
# Terminal-style output only. File reads, diffs and JSON keep every byte: trailing
# whitespace can be content there (and Edit's old_string must match exactly).
_KINDS = frozenset({
    "tool_result:command_output", "tool_result:log", "tool_result:test_output",
    "tool_result:search_results", "tool_result:error",
})


class Whitespace(Transform):
    name = "whitespace"
    lossless = True
    stage = "cleanup"  # runs before the main-stage transform and chains into it

    def applicable(self, block, label, ctx: Context) -> bool:
        return block.type == "tool_result" and label.kind in _KINDS and _text_only(block)

    def apply(self, block, label, ctx: Context) -> Result | None:
        text = _ANSI.sub("", block.text).replace("\r\n", "\n")
        lines = text.split("\n")
        terminated = len(lines) > 1 and lines[-1] == ""
        if terminated:
            lines.pop()
        # search results are lines of code: trailing whitespace there may be copied into an
        # Edit's old_string, so only presentation noise (ANSI, CR overwrites) is removed
        keep_trailing = label.kind == "tool_result:search_results"
        lines = [_last_overwrite(ln) for ln in lines]
        if not keep_trailing:
            lines = _collapse_blank_runs([ln.rstrip(" \t") for ln in lines])
        out = "\n".join(lines) + ("\n" if terminated else "")
        if out == block.text:
            return None
        return Result(content=out, lossless=True)


def _text_only(block) -> bool:
    """A string replacement would drop images or other non-text parts."""
    content = block.raw.get("content") if isinstance(block.raw, dict) else None
    return not isinstance(content, list) or all(
        isinstance(c, dict) and c.get("type") == "text" for c in content)


def _collapse_blank_runs(lines: list[str]) -> list[str]:
    """Replace each run of 3+ empty lines with a single empty line."""
    out: list[str] = []
    run = 0
    for ln in lines + [None]:  # sentinel flushes a trailing run
        if ln == "":
            run += 1
            continue
        out.extend([""] * (1 if run >= 3 else run))
        run = 0
        if ln is not None:
            out.append(ln)
    return out


def _last_overwrite(line: str) -> str:
    """What a terminal shows after carriage-return redraws: the last non-empty segment."""
    if "\r" not in line:
        return line
    segments = [s for s in line.split("\r") if s]
    return segments[-1] if segments else ""


register(Whitespace())
