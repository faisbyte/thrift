"""Ranking.

STDLIB ONLY: used by the hook.

``BM25Ranker`` takes the top FTS5 ``bm25()`` candidates and re-scores them
with code-specific signals (exact symbol match, query-term coverage, path
match, chunk kind, test-file demotion), then diversifies (no overlapping
ranges, at most N chunks per file).

Extension point: an embedding ranker can implement the same ``Ranker``
protocol, e.g. by taking ``BM25Ranker.candidates()`` and re-ordering them by
vector similarity, without touching callers.
"""

from __future__ import annotations

import math
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from typing import Protocol

from .tokenize import MAX_QUERY_TERMS, Query, fts_match_expr, parse_query, split_identifier, variants


@dataclass(frozen=True)
class Weights:
    # FTS5 bm25 column weights: symbol, path, signature, body
    col_symbol: float = 10.0
    col_path: float = 4.0
    col_sig: float = 3.0
    col_body: float = 1.0
    candidates: int = 100
    # re-scoring
    coverage_floor: float = 0.2  # score multiplier at zero coverage (tuned on dev split)
    exact_symbol: float = 0.6  # query identifier == symbol / qualname
    exact_word: float = 0.15  # plain query word == symbol
    symbol_parts: float = 0.25  # all parts of the symbol appear in the query
    path_match: float = 0.15
    test_penalty: float = 0.6
    aux_penalty: float = 0.7  # examples/, benchmarks/, docs/, changelogs
    kind_factor: dict = field(
        default_factory=lambda: {"module": 0.75, "window": 0.8, "section": 0.9, "impl": 0.9}
    )
    max_per_file: int = 2


DEFAULT_WEIGHTS = Weights()

_TEST_PATH = re.compile(
    r"(^|/)(tests?|__tests__|spec|specs|testing)/|(^|/)test_[^/]*$|_test\.\w+$|\.(test|spec)\.\w+$|Tests?\.java$"
)


_AUX_PATH = re.compile(
    r"(^|/)(examples?|samples?|demos?|benchmarks?|bench|docs?|translations?|i18n|locales?)/"
    r"|(^|/)(CHANGELOG|CHANGES|HISTORY|NEWS)[^/]*$|(^|/)README\.[a-z]{2}([_-][A-Za-z]{2})?\.\w+$",
    re.I,
)
_AUX_WORDS = frozenset({"example", "examples", "benchmark", "benchmarks", "docs", "documentation", "changelog"})


def is_test_path(path: str) -> bool:
    return bool(_TEST_PATH.search(path))


def is_aux_path(path: str) -> bool:
    """Non-core files: examples, benchmarks, docs, changelogs, translated READMEs."""
    return bool(_AUX_PATH.search(path))


@dataclass
class Hit:
    chunk_id: int
    path: str
    start_line: int
    end_line: int
    kind: str
    symbol: str | None
    qualname: str | None
    signature: str | None
    nlines: int
    score: float = 0.0
    coverage: float = 0.0
    exact: bool = False
    matched: tuple = ()
    struct_matched: int = 0  # query terms found in symbol/path/signature (not just body)

    @property
    def label(self) -> str:
        return self.qualname or self.symbol or self.kind


class Ranker(Protocol):
    def rank(self, query: str | Query, k: int = 8) -> list[Hit]: ...


_SQL = """
SELECT c.id, c.path, c.start_line, c.end_line, c.kind, c.symbol, c.qualname, c.signature,
       (length(c.text) - length(replace(c.text, char(10), ''))) + 1 AS nlines,
       f.symbol_terms, f.path_terms, f.sig_terms, f.body_terms,
       bm25(fts, ?, ?, ?, ?) AS bm
FROM fts f JOIN chunks c ON c.id = f.rowid
WHERE fts MATCH ?
ORDER BY bm
LIMIT ?
"""


class BM25Ranker:
    def __init__(self, conn: sqlite3.Connection, weights: Weights = DEFAULT_WEIGHTS):
        self.conn = conn
        self.w = weights

    def doc_freqs(self, terms: list[str]) -> dict[str, int]:
        try:
            marks = ",".join("?" * len(terms))
            return dict(self.conn.execute(f"SELECT term, doc FROM fts_vocab WHERE term IN ({marks})", terms))
        except sqlite3.Error:
            return {}

    def n_chunks(self) -> int:
        try:
            row = self.conn.execute("SELECT value FROM meta WHERE key = 'chunks'").fetchone()
            if row:
                return int(row[0])
            return self.conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        except (sqlite3.Error, ValueError):
            return 0

    def select_terms(self, query: Query, df: dict[str, int], max_terms: int = MAX_QUERY_TERMS) -> Query:
        """Keep at most ``max_terms`` terms: identifier-derived terms first,
        then the rest by rarity (document frequency) in this index."""
        if len(query.terms) <= max_terms:
            return query
        ident = [t for t in query.terms if t in query.identifiers]
        rest = sorted((t for t in query.terms if t not in query.identifiers), key=lambda t: df.get(t, 0) or 10**9)
        # unknown terms (df 0) can't match anything: put them last
        rest = [t for t in rest if df.get(t)] + [t for t in rest if not df.get(t)]
        keep = set((ident + rest)[:max_terms])
        terms = [t for t in query.terms if t in keep]
        return Query(terms, query.identifiers, query.words, query.ident_like_count, query.mentions_tests)

    def candidates(self, query: Query) -> list[sqlite3.Row]:
        if not query:
            return []
        w = self.w
        return self.conn.execute(
            _SQL,
            (w.col_symbol, w.col_path, w.col_sig, w.col_body, fts_match_expr(query), w.candidates),
        ).fetchall()

    def rank(self, query: str | Query, k: int = 8) -> list[Hit]:
        q = parse_query(query) if isinstance(query, str) else query
        if not q:
            return []
        df = self.doc_freqs(q.terms)
        q = self.select_terms(q, df)
        rows = self.candidates(q)
        if not rows:
            return []
        n = self.n_chunks() or len(rows)
        idf = {t: math.log(1 + n / (1 + df.get(t, 0))) for t in q.terms}
        hits = [self._score(q, r, rows[0]["bm"], idf) for r in rows]
        hits.sort(key=lambda h: h.score, reverse=True)
        return diversify(hits, k, self.w.max_per_file)

    def _score(self, q: Query, r: sqlite3.Row, best_bm: float, idf: dict[str, float]) -> Hit:
        w = self.w
        qterms = q.terms
        sym = set((r["symbol_terms"] or "").split())
        pth = set((r["path_terms"] or "").split())
        struct = sym | pth | set((r["sig_terms"] or "").split())
        alls = struct | set((r["body_terms"] or "").split())
        def hit_in(t: str, terms: set) -> bool:
            return t in terms or any(v in terms for v in variants(t))

        matched = tuple(
            t for t in qterms if hit_in(t, alls) or (len(t) >= 4 and any(s.startswith(t) for s in sym))
        )
        struct_matched = sum(1 for t in matched if hit_in(t, struct) or any(s.startswith(t) for s in sym))
        # IDF-weighted coverage: matching rare terms counts more than "rich"/"text"
        total_idf = sum(idf.get(t, 1.0) for t in qterms) or 1.0
        coverage = sum(idf.get(t, 1.0) for t in matched) / total_idf
        # bm25() is negative; best (most negative) candidate normalises to 1.0
        base = (r["bm"] / best_bm) if best_bm < 0 else 0.0
        score = base * (w.coverage_floor + (1 - w.coverage_floor) * coverage)

        symbol = (r["symbol"] or "").lower()
        qual = (r["qualname"] or "").lower()
        exact = False
        if symbol and (symbol in q.identifiers or qual in q.identifiers):
            score += w.exact_symbol
            exact = True
        elif symbol and symbol in q.words and len(symbol) >= 4:
            score += w.exact_word
        elif symbol:
            parts = split_identifier(r["symbol"])
            if len(parts) > 1 and all(p in qterms for p in parts):
                score += w.symbol_parts

        if qterms:
            score += w.path_match * (sum(1 for t in qterms if t in pth) / len(qterms))
        score *= w.kind_factor.get(r["kind"], 1.0)
        if not q.mentions_tests and is_test_path(r["path"]):
            score *= w.test_penalty
        elif is_aux_path(r["path"]) and not (q.words & _AUX_WORDS or set(qterms) & _AUX_WORDS):
            score *= w.aux_penalty

        return Hit(
            chunk_id=r["id"],
            path=r["path"],
            start_line=r["start_line"],
            end_line=r["end_line"],
            kind=r["kind"],
            symbol=r["symbol"],
            qualname=r["qualname"],
            signature=r["signature"],
            nlines=r["nlines"],
            score=score,
            coverage=coverage,
            exact=exact,
            matched=matched,
            struct_matched=struct_matched,
        )


def _overlap(a: Hit, b: Hit) -> bool:
    # containers span their whole class but their text is only the header,
    # so compare on the lines each chunk actually carries
    a_end = a.start_line + a.nlines - 1
    b_end = b.start_line + b.nlines - 1
    inter = min(a_end, b_end) - max(a.start_line, b.start_line) + 1
    if inter <= 0:
        return False
    return inter >= 0.5 * min(a.nlines, b.nlines)


def diversify(hits: list[Hit], k: int, max_per_file: int) -> list[Hit]:
    out: list[Hit] = []
    per_file: Counter = Counter()
    for h in hits:
        if per_file[h.path] >= max_per_file:
            continue
        if any(o.path == h.path and _overlap(o, h) for o in out):
            continue
        # windows of one long (split) definition: keep only the best one
        if h.qualname and any(o.path == h.path and o.qualname == h.qualname and o.kind == h.kind for o in out):
            continue
        out.append(h)
        per_file[h.path] += 1
        if len(out) >= k:
            break
    return out
