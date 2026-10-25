"""Compact plain-text renderers (token-frugal by design).

STDLIB ONLY: the hook uses :func:`hook_context`.
"""

from __future__ import annotations

import textwrap

from .rank import Hit
from .tokenize import Query, text_terms

SNIPPET_LINES = 6
SNIPPET_CHARS = 400
LINE_CHARS = 160
SYMBOL_MAX_LINES = 120
HEADER_LINES = 15
MEMBERS_MAX = 60
HOOK_MAX_ENTRIES = 6
HOOK_MAX_CHARS = 600


def _clip(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def pick_snippet(text: str, start_line: int, query: Query, max_lines: int = SNIPPET_LINES,
                 from_top: bool = False) -> list[tuple[int, str]]:
    """Choose the window of ``max_lines`` lines densest in query terms
    (or the first lines when ``from_top``, e.g. for exact symbol hits)."""
    lines = text.split("\n")
    n = len(lines)
    win = min(max_lines, n)
    best_i = 0
    if not from_top:
        qset = set(query.terms)
        scores = [len(qset.intersection(text_terms(ln).split())) for ln in lines]
        best = -1
        run = sum(scores[:win])
        for i in range(0, n - win + 1):
            if i > 0:
                run += scores[i + win - 1] - scores[i - 1]
            if run > best:
                best, best_i = run, i
    sel = lines[best_i : best_i + win]
    while sel and not sel[-1].strip():
        sel.pop()
    sel = textwrap.dedent("\n".join(sel)).split("\n")
    return [(start_line + best_i + j, ln) for j, ln in enumerate(sel)]


def _fmt_lines(numbered: list[tuple[int, str]], max_chars: int | None = None) -> str:
    out: list[str] = []
    used = 0
    width = len(str(numbered[-1][0])) if numbered else 1
    for no, ln in numbered:
        row = f"  {no:>{width}}: {_clip(ln.rstrip(), LINE_CHARS)}"
        if max_chars is not None and used + len(row) > max_chars and out:
            out.append("  …")
            break
        out.append(row)
        used += len(row)
    return "\n".join(out)


def hit_header(h: Hit) -> str:
    parts = [f"{h.path}:L{h.start_line}-{h.end_line}", h.kind]
    if h.qualname or h.symbol:
        parts.append(h.qualname or h.symbol or "")
    head = "  ".join(parts)
    if h.signature:
        head += "  " + _clip(h.signature, 100)
    return head


def search_results(query_text: str, query: Query, hits: list[Hit], texts: dict[int, str]) -> str:
    if not hits:
        return f'No matches for "{query_text}" in the thrift index. Try other identifiers or Grep.'
    out = [f'{len(hits)} matches for "{_clip(query_text, 80)}" (1-based line ranges; Read with offset/limit for more):']
    for h in hits:
        out.append(hit_header(h))
        text = texts.get(h.chunk_id)
        if text:
            out.append(_fmt_lines(pick_snippet(text, h.start_line, query, from_top=h.exact), SNIPPET_CHARS))
    return "\n".join(out)


def symbol_body(path: str, start: int, end: int, kind: str, label: str, lines: list[str]) -> str:
    total = end - start + 1
    shown = lines[: SYMBOL_MAX_LINES]
    head = f"{path}:L{start}-{end}  {kind}  {label}"
    body = _fmt_lines([(start + i, ln) for i, ln in enumerate(shown)])
    if total > len(shown):
        rest = start + len(shown)
        body += f"\n  … {total - len(shown)} more lines: Read {path} offset={rest} limit={end - rest + 1}"
    return head + "\n" + body


def symbol_outline(path: str, start: int, end: int, kind: str, label: str, header: list[str],
                   members: list[tuple]) -> str:
    head = f"{path}:L{start}-{end}  {kind}  {label}  ({end - start + 1} lines; header and members shown)"
    body = _fmt_lines([(start + i, ln) for i, ln in enumerate(header[:HEADER_LINES])])
    rows = [f"  L{s}-{e}  {_clip(sig or sym or k, 110)}" for s, e, k, sym, sig in members[:MEMBERS_MAX]]
    if len(members) > MEMBERS_MAX:
        rows.append(f"  … {len(members) - MEMBERS_MAX} more members")
    tail = f"members ({len(members)}); use get_symbol('{label}.<name>') for one:\n" + "\n".join(rows) if rows else ""
    return "\n".join(x for x in (head, body, tail) if x)


def candidates_list(name: str, rows: list[tuple]) -> str:
    out = [f'"{name}" is ambiguous ({len(rows)} definitions). Ask for one by qualified name (Class.method) or "path:name":']
    for path, start, end, kind, label, sig in rows[:10]:
        line = f"{path}:L{start}-{end}  {kind}  {label}"
        if sig:
            line += "  " + _clip(sig, 90)
        out.append(line)
    if len(rows) > 10:
        out.append(f"… {len(rows) - 10} more")
    return "\n".join(out)


def related(path: str, imports: list[str], importers: list[str], cap: int = 25) -> str:
    def fmt(lst: list[str]) -> str:
        if not lst:
            return "none found"
        s = ", ".join(lst[:cap])
        return s + (f", … +{len(lst) - cap} more" if len(lst) > cap else "")

    return f"{path}\nimports ({len(imports)}): {fmt(imports)}\nimported by ({len(importers)}): {fmt(importers)}"


def hook_context(hits: list[Hit]) -> str:
    """One compact pointer line grouped by file, roughly 150 tokens or fewer."""
    by_file: dict[str, list[str]] = {}
    count = 0
    for h in hits:
        if count >= HOOK_MAX_ENTRIES:
            break
        by_file.setdefault(h.path, []).append(f"L{h.start_line}-{h.end_line} {_clip(h.label, 60)}")
        count += 1
    prefix = "thrift index matches for this prompt (file, line range, symbol): "
    groups = [f"{p} " + ", ".join(items) for p, items in by_file.items()]
    while groups and len(prefix) + len("; ".join(groups)) > HOOK_MAX_CHARS:
        groups.pop()
    return prefix + "; ".join(groups) if groups else ""
