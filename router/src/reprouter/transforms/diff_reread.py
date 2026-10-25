"""diff_reread: render a re-read of a file as a diff against the earlier Read of it.

Lossless relative to the earlier read: every hunk carries a unified-diff header
("@@ -a,b +c,d @@") giving old and new positions, kept new-file lines are shown
verbatim in Read's "N\\t" format with their NEW line numbers, and removed lines
are shown as "-\\t<old text>". Lines outside the hunks are unchanged from the
earlier read, so (earlier read + this render) rebuilds the new file exactly.
"""

from __future__ import annotations

import difflib
import re

from .base import Result, Transform, register
from .dedup import text_only

CONTEXT = 1
MAX_RATIO = 0.70
_NUM = re.compile(r" *(\d+)\t")


def split_numbered(text: str):
    """Split Read output into (first line number, raw numbered lines, contents, trailing parts).

    The numbered body is the leading run of "N\\t" lines; anything after it
    (e.g. a trailing <system-reminder>) is returned verbatim as split parts.
    """
    parts = text.split("\n")
    first, raws, contents = None, [], []
    for p in parts:
        m = _NUM.match(p)
        if not m:
            break
        if first is None:
            first = int(m.group(1))
        raws.append(p)
        contents.append(p[m.end():])
    return first, raws, contents, parts[len(raws):]


def _range(first: int, start: int, stop: int) -> str:
    # Both bodies are non-empty and hunks carry context, so ranges are never empty.
    length = stop - start
    return str(first + start) if length == 1 else f"{first + start},{length}"


def _same_window(a: dict, b: dict) -> bool:
    return a.get("offset") == b.get("offset") and a.get("limit") == b.get("limit")


def _earlier_read(block, ctx):
    """Most recent successful Read of the same file and window preceding ``block``."""
    path = block.tool_input.get("file_path")
    for prev, _ in reversed(ctx.prior):
        if (prev.type == "tool_result" and prev.tool_name == "Read" and not prev.is_error
                and prev.tool_input.get("file_path") == path and _same_window(prev.tool_input, block.tool_input)):
            return prev
    return None


class DiffReread(Transform):
    name = "diff_reread"
    lossless = True

    def applicable(self, block, label, ctx):
        return (block.type == "tool_result" and block.tool_name == "Read" and not block.is_error
                and bool(block.tool_input.get("file_path")) and text_only(block))

    def apply(self, block, label, ctx):
        prev = _earlier_read(block, ctx)
        if prev is None or prev.text == block.text:
            return None
        old_first, old_raws, old_c, _ = split_numbered(prev.text)
        new_first, new_raws, new_c, rest = split_numbered(block.text)
        if not old_raws or not new_raws or old_c == new_c:
            return None
        out = [f"[reprouter: re-read of {block.tool_input['file_path']}; changed since the earlier read "
               f"(tool_use_id {prev.tool_use_id}). Unchanged lines omitted; lines below show the NEW file "
               "with line numbers]"]
        sm = difflib.SequenceMatcher(None, old_c, new_c, autojunk=False)
        for group in sm.get_grouped_opcodes(CONTEXT):
            i1, i2, j1, j2 = group[0][1], group[-1][2], group[0][3], group[-1][4]
            out.append(f"@@ -{_range(old_first, i1, i2)} +{_range(new_first, j1, j2)} @@")
            for tag, a1, a2, b1, b2 in group:
                if tag in ("replace", "delete"):
                    out += ["-\t" + line for line in old_c[a1:a2]]
                if tag != "delete":
                    out += new_raws[b1:b2]
        content = "\n".join(out + rest)
        if len(content) > MAX_RATIO * len(block.text):
            return None
        return Result(content=content, lossless=True, note=f"diff against {prev.tool_use_id}")


register(DiffReread())
