"""Render dense, non-exact tool output as PNG pages (pxpipe-style), with a text
factsheet of exact strings (hashes, UUIDs, paths, numbers) to counter the
known failure mode of vision models: silently confabulating exact strings.

Rendering uses the Spleen bitmap font (BSD-2-Clause, vendored in ../fonts).
Output is deterministic for the same input and Pillow version.
"""

from __future__ import annotations

import base64
import io
import os
import re
import struct
import textwrap
from functools import lru_cache
from pathlib import Path

from PIL import BdfFontFile, Image, ImageDraw, ImageFont

from ..classify import Label
from ..request import Block
from .base import Context, Result, Transform, register

FONT_DIR = Path(__file__).resolve().parent.parent / "fonts"
CELLS = {"6x12": (6, 12), "8x16": (8, 16)}
PAD = 4
TABSIZE = 4

_FACT_PATTERNS = [
    re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"),
    re.compile(r"(?<![0-9A-Za-z])(?=[0-9a-fA-F]*\d)(?=[0-9a-fA-F]*[a-fA-F])[0-9a-fA-F]{7,64}(?![0-9A-Za-z])"),
    re.compile(r"(?<![\w.])/[\w.\-]+(?:/[\w.\-]+)+|\b[\w.\-]+/[\w.\-/]*\.[A-Za-z0-9]{1,8}\b"),
    re.compile(r"\b[a-z]{2,8}_[A-Za-z0-9]{10,}\b|\b[A-Z][A-Z0-9]{1,9}-\d{2,}\b"),
    re.compile(r"(?<![\w.])-?\d+\.\d+(?![\w.])|(?<![\w.])\d{4,}(?![\w.])"),
]


@lru_cache(maxsize=None)
def _font(cell: str) -> ImageFont.ImageFont:
    cache = Path(os.path.expanduser("~/.cache/reprouter/fonts"))
    cache.mkdir(parents=True, exist_ok=True)
    base = cache / f"spleen-{cell}"
    if not (base.with_suffix(".pil")).exists():
        with open(FONT_DIR / f"spleen-{cell}.bdf", "rb") as fp:
            BdfFontFile.BdfFontFile(fp).save(str(base))
    return ImageFont.load(str(base.with_suffix(".pil")))


def _wrap(text: str, cols: int) -> list[str]:
    rows: list[str] = []
    for line in text.expandtabs(TABSIZE).split("\n"):
        if not line:
            rows.append("")
            continue
        rows.extend(line[i:i + cols] for i in range(0, len(line), cols))
    return rows


def render_pages(text: str, cell: str = "6x12", cols: int = 160, rows: int = 120) -> list[Image.Image]:
    cw, ch = CELLS[cell]
    font = _font(cell)
    lines = _wrap(text, cols)
    pages = []
    for start in range(0, max(len(lines), 1), rows):
        chunk = lines[start:start + rows]
        img = Image.new("L", (cols * cw + 2 * PAD, len(chunk) * ch + 2 * PAD), 255)
        draw = ImageDraw.Draw(img)
        for i, ln in enumerate(chunk):
            if ln:
                draw.text((PAD, PAD + i * ch), ln, font=font, fill=0)
        pages.append(img)
    return pages


def _png_b64(img: Image.Image) -> str:
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def png_size(b64: str) -> tuple[int, int]:
    head = base64.b64decode(b64[:44])  # signature + IHDR
    return struct.unpack(">II", head[16:24])


def extract_factsheet(text: str, max_items: int = 96) -> list[str]:
    found: list[tuple[int, str]] = []
    seen: set[str] = set()
    for pat in _FACT_PATTERNS:
        for m in pat.finditer(text):
            s = m.group(0)
            if s not in seen:
                seen.add(s)
                found.append((m.start(), s))
    found.sort()
    return [s for _, s in found[:max_items]]


class ImageTransform(Transform):
    name = "image"
    lossless = False
    min_chars = 6000
    cell = "6x12"
    cols = 160
    rows = 120
    kinds = {"tool_result:command_output", "tool_result:log", "tool_result:test_output",
             "tool_result:search_results", "tool_result:file_read"}

    def applicable(self, block: Block, label: Label, ctx: Context) -> bool:
        return (block.type == "tool_result" and not block.is_error and label.kind in self.kinds
                and "code_edit_target" not in label.risks and len(block.text) >= self.min_chars)

    def apply(self, block: Block, label: Label, ctx: Context) -> Result | None:
        pages = render_pages(block.text, self.cell, self.cols, self.rows)
        content: list[dict] = [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": _png_b64(p)}}
            for p in pages
        ]
        facts = extract_factsheet(block.text)
        n_lines = block.text.count("\n") + 1
        note = (f"[reprouter: the tool output above ({n_lines} lines) is rendered as {len(pages)} image(s) to save "
                f"tokens and may contain transcription errors. Exact strings from it, verbatim:]\n"
                + "\n".join(facts)
                + "\n[Re-run the command if you need any other text verbatim.]")
        content.append({"type": "text", "text": note})
        return Result(content=content, lossless=False)


register(ImageTransform())
