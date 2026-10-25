"""alias: replace long, repeated directory prefixes in tool results with short aliases.

Example: "/private/var/folders/05/.../edit-padding-css__baseline__0/src/app.css"
becomes "§1/src/app.css". The first block (in prompt order) that uses an alias
starts with a legend line defining it, shell-variable style:

    [reprouter aliases: §1=/private/var/folders/05/.../edit-padding-css__baseline__0]

so "§1/x" means "<prefix>/x". Later blocks use the alias without a legend.

The alias table is a pure function of the request prefix (every block before
the current one, plus the current one), replayed block by block so that it only
ever grows: an alias, once defined, keeps its meaning for the rest of the
conversation and every earlier block re-renders byte-identically next turn.
Lossless: ``expand_aliases`` exactly inverts ``apply_aliases`` (blocks that
contain the sigil character are never aliased), and is also how the proxy maps
aliases the model writes in tool_use inputs back to real paths.
"""

from __future__ import annotations

import re
from collections import Counter

from .base import Result, Transform, register
from .dedup import text_only

_PATH_CHAR = r"[\w.\-@+~%#]"
_BOUNDARY = r"(?<![\w.\-@+~%#/])"  # a path starts here (not mid-way through another path)
_PATH = re.compile(_BOUNDARY + r"/(?:" + _PATH_CHAR + r"+/)+")  # directory part of an absolute path
MIN_PREFIX = 24
MIN_COUNT = 3
MAX_ALIASES = 8
SIGILS = ("§", "¤")


def _prefixes(d: str) -> list[str]:
    """Directory prefixes of directory path ``d`` that are long enough to alias."""
    return [d[: k + 1] for k, ch in enumerate(d) if ch == "/" and k + 1 >= MIN_PREFIX]


def _choose(counts: Counter, uncovered: Counter, first: dict, slots: int) -> list[str]:
    """Greedily pick up to ``slots`` prefixes by uncovered-uses x length (>= MIN_COUNT uses).

    Each pick removes the occurrences it covers from ``uncovered`` and from the
    counts of every other candidate, so ancestors/descendants of a chosen
    prefix only qualify on their own remaining uses.
    """
    new: list[str] = []
    while len(new) < slots:
        eligible = [p for p, c in counts.items() if c >= MIN_COUNT]
        if not eligible:
            break
        best = min(eligible, key=lambda p: (-counts[p] * len(p), first[p], p))
        new.append(best)
        for d in [d for d in uncovered if d.startswith(best)]:
            n = uncovered.pop(d)
            for p in _prefixes(d):
                counts[p] -= n
    return sorted(new, key=lambda p: first[p])


def iter_tables(texts):
    """Yield the alias table (alias -> prefix) after each text, in order.

    Tables only ever grow: once assigned, an alias keeps its prefix and number,
    so a legend shown in an earlier block stays valid for every later one. At
    each step, prefixes used >= MIN_COUNT times (and not already covered by an
    alias) are chosen greedily by count x length; new aliases are numbered after
    existing ones, in order of first appearance. The sigil is fixed when the
    first alias is created: the first of SIGILS absent from all text so far; if
    none is free by then, no aliases are ever created for the request.
    """
    table: dict[str, str] = {}
    sigil = None
    seen: set[str] = set()  # sigils that occurred before the table was created
    uncovered = Counter()  # directory part of each path occurrence not under an alias -> uses
    counts = Counter()  # candidate prefix -> uncovered uses
    first: dict[str, tuple] = {}  # candidate prefix -> position of first appearance
    for ti, text in enumerate(texts):
        if sigil is None:
            seen.update(s for s in SIGILS if s in text)
            if len(seen) == len(SIGILS):
                yield {}
                continue
        if len(table) < MAX_ALIASES:
            chosen = tuple(table.values())
            for m in _PATH.finditer(text):
                d = m.group(0)
                if chosen and d.startswith(chosen):
                    continue
                uncovered[d] += 1
                for p in _prefixes(d):
                    counts[p] += 1
                    first.setdefault(p, (ti, m.start(), len(p)))
            new = _choose(counts, uncovered, first, MAX_ALIASES - len(table))
            if new and sigil is None:
                sigil = SIGILS[0] if SIGILS[0] not in seen else SIGILS[1]
            for p in new:
                table[f"{sigil}{len(table) + 1}/"] = p
        yield dict(table)


def build_table(texts: list[str]) -> dict[str, str]:
    """Alias table, e.g. {"§1/": "/long/dir/prefix/"}, for a request prefix's texts in prompt order."""
    table: dict[str, str] = {}
    for table in iter_tables(texts):
        pass
    return table


def _number(token: str) -> int:
    return int(token[1:-1])


def apply_aliases(text: str, table: dict[str, str]) -> tuple[str, list[str]]:
    """Replace each table prefix that starts a path in ``text`` with its alias.

    The longest prefix wins where several match. Returns (aliased text, aliases
    used, in number order). Exactly inverted by expand_aliases as long as
    ``text`` does not contain the table's sigil.
    """
    if not table:
        return text, []
    by_prefix = {p: tok for tok, p in table.items()}
    alternatives = "|".join(re.escape(p) for p in sorted(by_prefix, key=len, reverse=True))
    pattern = re.compile(_BOUNDARY + "(?:" + alternatives + ")")
    used = set()

    def sub(m):
        tok = by_prefix[m.group(0)]
        used.add(tok)
        return tok

    out = pattern.sub(sub, text)
    return out, sorted(used, key=_number)


def expand_aliases(value, table: dict[str, str]):
    """Replace aliases with their prefixes inside str / list / dict values (dict keys are kept).

    "§1/rest" expands to "<prefix>rest"; a bare "§1" (not followed by "/" or a
    digit) expands to the prefix without its trailing slash. Returns new objects.
    """
    if not table:
        return value
    names = sorted((tok[:-1] for tok in table), key=len, reverse=True)
    pattern = re.compile("(" + "|".join(map(re.escape, names)) + r")(?!\d)(/?)")

    def sub(m):
        prefix = table[m.group(1) + "/"]
        return prefix if m.group(2) else prefix[:-1]

    def walk(v):
        if isinstance(v, str):
            return pattern.sub(sub, v)
        if isinstance(v, list):
            return [walk(x) for x in v]
        if isinstance(v, dict):
            return {k: walk(x) for k, x in v.items()}
        return v

    return walk(value)


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)


def block_text(block) -> str:
    """Text a block contributes to alias counting: its text, or for tool_use its input strings."""
    if block.type == "tool_use":
        return "\n".join(_strings(block.tool_input))
    return block.text


def _eligible(block) -> bool:
    return block.type == "tool_result" and text_only(block)


def _aliased(block, table: dict[str, str]) -> tuple[str, list[str]]:
    """(aliased text, aliases used) for ``block`` under ``table``; (text, []) if it would not be rewritten."""
    if (not table or not _eligible(block) or next(iter(table))[0] in block.text
            or not any(p in block.text for p in table.values())):
        return block.text, []
    return apply_aliases(block.text, table)


class Alias(Transform):
    name = "alias"
    lossless = True

    def applicable(self, block, label, ctx):
        return _eligible(block)

    def apply(self, block, label, ctx):
        prior = [b for b, _ in ctx.prior]
        tables = list(iter_tables([block_text(b) for b in prior] + [block.text]))
        defined: set[str] = set()  # aliases whose legend an earlier block already carries
        for b, table in zip(prior, tables):
            if not defined.issuperset(table):
                defined.update(_aliased(b, table)[1])
        table = tables[-1]
        aliased, used = _aliased(block, table)
        if not used:
            return None
        new = [tok for tok in used if tok not in defined]
        if not new:
            return Result(content=aliased, lossless=True)
        legend = " ".join(f"{tok[:-1]}={table[tok][:-1]}" for tok in new)
        return Result(content=f"[reprouter aliases: {legend}]\n{aliased}", lossless=True, note="defines " + legend)


register(Alias())
