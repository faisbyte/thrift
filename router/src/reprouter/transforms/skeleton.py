"""Signatures-only skeleton of a Read result for files outside the project."""

from __future__ import annotations

import re

from thriftctx.chunker import chunk_file, detect_language

from .base import Context, Result, Transform, register

_NUMBERED = re.compile(r" *(\d+)\t(.*)", re.S)
FOOTER = "[reprouter: signatures only (file outside the project). Read with offset/limit to see bodies.]"
CODE_LANGS = frozenset({"python", "javascript", "typescript", "tsx", "rust", "java", "c", "cpp"})  # tree-sitter
MIN_LINES = 80
MAX_RATIO = 0.5  # decline unless the skeleton is at most half the input
SIG_MAX_LINES = 12  # longest header (decorators + signature) we try to keep whole
_NON_DEFS = frozenset({"module", "window", "section"})  # chunker kinds that are not definitions


class Skeleton(Transform):
    """Aider-style map of a library/vendored file: definition headers with their
    original line numbers, one "⋮" per elided gap. Never used on project files,
    which may be about to be edited and must stay exact."""

    name = "skeleton"
    lossless = False

    def applicable(self, block, label, ctx: Context) -> bool:
        if block.type != "tool_result" or label.kind != "tool_result:file_read":
            return False
        if "code_edit_target" in label.risks or not _text_only(block):
            return False
        return detect_language(block.tool_input.get("file_path") or "") in CODE_LANGS

    def apply(self, block, label, ctx: Context) -> Result | None:
        parsed = _parse_read(block.text)
        if parsed is None or len(parsed[0]) < MIN_LINES:
            return None
        lines, contents, tail = parsed
        source = "\n".join(contents) + "\n"
        lang, chunks = chunk_file(block.tool_input["file_path"], source.encode("utf-8"))
        keep = _definition_rows(chunks, contents, lang)
        if not keep:
            return None  # nothing but module-level code: a skeleton would say nothing
        out = _render(lines, keep)
        out.append(FOOTER)
        if any(t.strip() for t in tail):
            out.extend(tail)  # e.g. a trailing <system-reminder>
        content = "\n".join(out)
        if len(content) > MAX_RATIO * len(block.text):
            return None
        return Result(content=content, lossless=False)


def _parse_read(text: str) -> tuple[list[str], list[str], list[str]] | None:
    """Split Read output into (numbered lines, their contents, trailing text).

    Declines (None) unless the numbered lines form one contiguous, consecutively
    numbered block at the start, since line numbers are recomputed from positions.
    """
    raw = text.split("\n")
    lines, contents = [], []
    first = None
    for ln in raw:
        m = _NUMBERED.match(ln)
        if m is None:
            break
        first = int(m.group(1)) if first is None else first
        if int(m.group(1)) != first + len(lines):
            return None
        lines.append(ln)
        contents.append(m.group(2))
    tail = raw[len(lines):]
    if any(_NUMBERED.match(t) for t in tail):
        return None
    return lines, contents, tail


def _definition_rows(chunks, contents: list[str], lang: str) -> set[int]:
    keep: set[int] = set()
    seen: dict[tuple, list[tuple[int, int]]] = {}
    for c in chunks:
        if c.kind in _NON_DEFS:
            continue
        key = (c.kind, c.qualname, c.symbol)
        # definitions longer than the chunker's window come back as several
        # overlapping chunks; only the first one starts at the real header
        continuation = any(s <= c.start_line <= e for s, e in seen.get(key, ()))
        seen.setdefault(key, []).append((c.start_line, c.end_line))
        if not continuation:
            keep.update(_signature_rows(contents, c.start_line - 1, c.end_line - 1, lang))
    return keep


def _render(lines: list[str], keep: set[int]) -> list[str]:
    out: list[str] = []
    for k, ln in enumerate(lines):
        if k in keep:
            out.append(ln)  # verbatim, so the original "N\t" prefix survives
        elif not out or out[-1] != "⋮":
            out.append("⋮")
    return out


def _text_only(block) -> bool:
    """A string replacement would drop images or other non-text parts."""
    content = block.raw.get("content") if isinstance(block.raw, dict) else None
    return not isinstance(content, list) or all(
        isinstance(c, dict) and c.get("type") == "text" for c in content)


def _signature_rows(src: list[str], start: int, end: int, lang: str) -> range:
    """Rows of a definition's header: decorators/attributes through the line opening its body.

    Leading comments are skipped. Without a body opener the whole definition is kept
    if it fits in SIG_MAX_LINES (one-liners); otherwise only its first line.
    """
    comment = ("#",) if lang == "python" else ("//", "/*", "*")
    first = start
    while first < end and (not src[first].strip() or src[first].lstrip().startswith(comment)):
        first += 1
    last_allowed = min(end, first + SIG_MAX_LINES - 1)
    for row in range(first, last_allowed + 1):
        stripped = src[row].strip()
        if stripped.startswith(("@", "#[")):
            continue  # decorator / annotation / attribute: the definition follows
        if _opens_body(stripped, lang):
            return range(first, row + 1)
    return range(first, last_allowed + 1) if last_allowed == end else range(first, first + 1)


def _opens_body(line: str, lang: str) -> bool:
    if lang == "python":
        return re.sub(r"\s+#.*$", "", line).endswith(":")
    code = re.sub(r"\s*//.*$", "", line)
    return "{" in code or code.endswith(";")


register(Skeleton())
