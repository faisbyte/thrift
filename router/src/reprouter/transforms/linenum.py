"""Line-number prefixes on Read results: compact (lossless) and sparse (lossy)."""

from __future__ import annotations

import re

from .base import Context, Result, Transform, register

_PADDED = re.compile(r"^ +(\d+\t)", re.M)
_NUMBERED = re.compile(r" *(\d+)\t(.*)", re.S)
SPARSE_MIN_LINES = 20


def _is_text_read(block, label) -> bool:
    """A text-only Read result (a string replacement would drop image parts)."""
    if block.type != "tool_result" or label.kind != "tool_result:file_read":
        return False
    content = block.raw.get("content") if isinstance(block.raw, dict) else None
    return not isinstance(content, list) or all(
        isinstance(c, dict) and c.get("type") == "text" for c in content)


class LinenumCompact(Transform):
    """Older Claude Code left-pads Read line numbers ("     1\\t"); drop the padding."""

    name = "linenum_compact"
    lossless = True

    def applicable(self, block, label, ctx: Context) -> bool:
        return _is_text_read(block, label)

    def apply(self, block, label, ctx: Context) -> Result | None:
        out = _PADDED.sub(r"\1", block.text)
        if out == block.text:
            return None
        return Result(content=out, lossless=True)


class LinenumSparse(Transform):
    """Keep the number only on the first line, multiples of ten, and the last line.

    Other lines keep a bare leading tab so the reader can still count from the
    nearest anchor. Never used on files that may be edited (exact line refs matter).
    """

    name = "linenum_sparse"
    lossless = False

    def applicable(self, block, label, ctx: Context) -> bool:
        return _is_text_read(block, label) and "code_edit_target" not in label.risks

    def apply(self, block, label, ctx: Context) -> Result | None:
        lines = block.text.split("\n")
        numbered = [(i, m) for i, ln in enumerate(lines) if (m := _NUMBERED.match(ln))]
        if len(numbered) < SPARSE_MIN_LINES:
            return None
        first = int(numbered[0][1].group(1))
        if any(int(m.group(1)) != first + k for k, (_, m) in enumerate(numbered)):
            return None  # the reader counts lines between anchors; gaps would mislead
        final = len(numbered) - 1
        for k, (i, m) in enumerate(numbered):
            n = first + k
            anchor = k in (0, final) or n % 10 == 0
            lines[i] = (f"{n}\t" if anchor else "\t") + m.group(2)
        return Result(content="\n".join(lines), lossless=False)


register(LinenumCompact())
register(LinenumSparse())
