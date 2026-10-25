"""High-level operations shared by the CLI and the MCP server."""

from __future__ import annotations

import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from . import format as fmt
from . import store
from .imports import Resolver
from .indexer import IndexStats, index_project
from .rank import BM25Ranker, is_test_path
from .tokenize import parse_query

DEF_KIND_ORDER = {
    "class": 0, "struct": 0, "interface": 0, "trait": 0, "enum": 0, "type": 1,
    "function": 0, "method": 0, "macro": 1, "impl": 2, "section": 3, "module": 4, "window": 4,
}


CONTAINER_KINDS = frozenset({"class", "struct", "interface", "trait", "impl", "enum"})


def resolve_root(explicit: str | None = None) -> Path:
    """Project root: explicit arg > THRIFT_PROJECT > CLAUDE_PROJECT_DIR > nearest index > cwd."""
    for cand in (explicit, os.environ.get("THRIFT_PROJECT"), os.environ.get("CLAUDE_PROJECT_DIR")):
        if cand and "${" not in cand and Path(cand).is_dir():
            return Path(cand).resolve()
    found = store.find_project_root()
    return found or Path.cwd().resolve()


class Service:
    """Holds the project root and keeps the index fresh between calls."""

    def __init__(self, root: str | os.PathLike, refresh_interval: float = 5.0,
                 first_build_budget: float = 60.0):
        self.root = Path(root).resolve()
        self.refresh_interval = refresh_interval
        self.first_build_budget = first_build_budget
        self._last_refresh = 0.0

    # -- freshness ---------------------------------------------------------

    def ensure_fresh(self) -> str | None:
        """Incrementally refresh at most every ``refresh_interval`` seconds.

        Builds the index on first use (time-capped). Returns a note for the
        caller when the index is missing/partial, else None.
        """
        now = time.monotonic()
        missing = not store.db_path(self.root).is_file()
        if not missing and now - self._last_refresh < self.refresh_interval:
            return None
        budget = self.first_build_budget if missing else 20.0
        stats: IndexStats = index_project(self.root, time_budget=budget, lock_wait=0.0)
        self._last_refresh = time.monotonic()
        if stats.busy and missing:
            return "thrift: the index is being built by another process; results may be empty. Retry shortly."
        if not stats.complete:
            return "thrift: index is partial (time budget hit); run /thrift:ctx-index to finish."
        return None

    def _conn(self) -> sqlite3.Connection:
        return store.open_readonly(self.root, busy_ms=2000)

    # -- operations --------------------------------------------------------

    def search(self, query: str, k: int = 8) -> str:
        note = self.ensure_fresh()
        k = max(1, min(int(k), 20))
        q = parse_query(query)
        if not q:
            return "Query has no searchable terms; include identifiers or concrete words."
        with closing(self._conn()) as conn:
            hits = BM25Ranker(conn).rank(q, k)
            texts = {}
            if hits:
                ids = [h.chunk_id for h in hits]
                rows = conn.execute(
                    f"SELECT id, text FROM chunks WHERE id IN ({','.join('?' * len(ids))})", ids
                ).fetchall()
                texts = {r["id"]: r["text"] for r in rows}
        out = fmt.search_results(query, q, hits, texts)
        return f"{note}\n{out}" if note else out

    def symbol(self, name: str) -> str:
        note = self.ensure_fresh()
        name = name.strip()
        path_filter = None
        if ":" in name and not name.startswith(":") and "::" not in name.split(":", 1)[0]:
            left, right = name.rsplit(":", 1)
            if "." in left or "/" in left:
                path_filter, name = left, right
        qual = name.replace("::", ".")
        last = qual.rsplit(".", 1)[-1]
        with closing(self._conn()) as conn:
            rows = conn.execute(
                "SELECT path, start_line, end_line, kind, symbol, qualname, signature, text FROM chunks"
                " WHERE qualname = ? COLLATE NOCASE OR symbol = ? COLLATE NOCASE"
                " OR qualname LIKE ? ESCAPE '\\'",
                (qual, last if "." not in qual else qual, "%." + qual.replace("%", "\\%").replace("_", "\\_")),
            ).fetchall()
        if path_filter:
            rows = [r for r in rows if r["path"].endswith(path_filter) or path_filter in r["path"]]
        if "." in qual:
            rows = [r for r in rows if (r["qualname"] or "").lower().endswith(qual.lower())]
        if not rows:
            msg = f'No definition named "{name}" in the thrift index. Try search_code("{name}").'
            return f"{note}\n{msg}" if note else msg

        # merge split windows of the same definition
        groups: dict[tuple, dict] = {}
        for r in rows:
            key = (r["path"], r["qualname"] or r["symbol"], r["kind"])
            g = groups.get(key)
            if g is None:
                groups[key] = g = {"path": r["path"], "start": r["start_line"], "end": r["end_line"],
                                   "kind": r["kind"], "label": r["qualname"] or r["symbol"],
                                   "sig": r["signature"], "symbol": r["symbol"] or "", "text": [r["text"]]}
            else:
                g["start"] = min(g["start"], r["start_line"])
                g["end"] = max(g["end"], r["end_line"])

        def rank_key(g):
            return (
                0 if g["symbol"] == last or g["label"] == qual else 1,
                1 if is_test_path(g["path"]) else 0,
                DEF_KIND_ORDER.get(g["kind"], 3),
                len(g["path"]),
                g["start"],
            )

        ordered = sorted(groups.values(), key=rank_key)
        if len(ordered) > 3:
            out = fmt.candidates_list(
                name, [(g["path"], g["start"], g["end"], g["kind"], g["label"], g["sig"]) for g in ordered]
            )
            return f"{note}\n{out}" if note else out
        best = ordered[0]
        span = best["end"] - best["start"] + 1
        if best["kind"] in CONTAINER_KINDS and span > fmt.SYMBOL_MAX_LINES:
            # big class: header + member outline instead of a wall of code
            with closing(self._conn()) as conn:
                members = conn.execute(
                    "SELECT start_line, end_line, kind, symbol, qualname, signature FROM chunks"
                    " WHERE path = ? AND qualname LIKE ? ESCAPE '\\' AND start_line >= ? AND end_line <= ?"
                    " ORDER BY start_line",
                    (best["path"], best["label"].replace("_", "\\_") + ".%", best["start"], best["end"]),
                ).fetchall()
            depth = best["label"].count(".") + 1
            seen: set = set()
            outline = []
            for m in members:
                if (m["qualname"] or "").count(".") != depth or m["qualname"] in seen:
                    continue
                seen.add(m["qualname"])
                outline.append((m["start_line"], m["end_line"], m["kind"], m["symbol"], m["signature"]))
            out = fmt.symbol_outline(best["path"], best["start"], best["end"], best["kind"], best["label"],
                                     best["text"][0].split("\n"), outline)
        else:
            lines = self._read_lines(best["path"], best["start"], best["end"]) or "\n".join(best["text"]).split("\n")
            out = fmt.symbol_body(best["path"], best["start"], best["end"], best["kind"], best["label"], lines)
        if len(ordered) > 1:
            others = "; ".join(f"{g['path']}:L{g['start']}-{g['end']} {g['kind']} {g['label']}" for g in ordered[1:])
            out += f"\nOther definitions: {others}"
        return f"{note}\n{out}" if note else out

    def _read_lines(self, rel: str, start: int, end: int) -> list[str] | None:
        try:
            with open(self.root / rel, encoding="utf-8", errors="replace") as f:
                lines = f.read().split("\n")
        except OSError:
            return None
        return lines[start - 1 : end]

    def related(self, path: str) -> str:
        note = self.ensure_fresh()
        rel = path
        p = Path(path)
        if p.is_absolute():
            try:
                rel = p.resolve().relative_to(self.root).as_posix()
            except ValueError:
                pass
        while rel.startswith("./"):
            rel = rel[2:]
        with closing(self._conn()) as conn:
            files = {r["path"]: r["lang"] for r in conn.execute("SELECT path, lang FROM files")}
            if rel not in files:
                matches = [f for f in files if f.endswith("/" + rel) or f == rel]
                if len(matches) != 1:
                    msg = (f'"{path}" is not in the index.' if not matches else
                           f'"{path}" is ambiguous: ' + ", ".join(matches[:10]))
                    return f"{note}\n{msg}" if note else msg
                rel = matches[0]
            resolver = Resolver(list(files))
            lang = files[rel]
            imports: list[str] = []
            for (spec,) in conn.execute("SELECT spec FROM imports WHERE path = ?", (rel,)):
                for dst in resolver.resolve(rel, lang, spec):
                    if dst not in imports:
                        imports.append(dst)
            # importers: cheap textual prefilter on the target's stem, then resolve
            stem = Path(rel).stem if Path(rel).stem not in ("__init__", "index", "mod") else Path(rel).parent.name
            importers: list[str] = []
            for src, spec in conn.execute(
                "SELECT path, spec FROM imports WHERE spec LIKE ? AND path != ?", (f"%{stem}%", rel)
            ):
                if src in importers:
                    continue
                if rel in resolver.resolve(src, files.get(src), spec):
                    importers.append(src)
        out = fmt.related(rel, imports, sorted(importers, key=lambda f: (is_test_path(f), f)))
        return f"{note}\n{out}" if note else out

    def stats(self) -> str:
        if not store.db_path(self.root).is_file():
            return f"No thrift index at {store.db_path(self.root)}"
        with closing(self._conn()) as conn:
            meta = store.get_meta(conn)
            nfiles = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
            nchunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
            langs = conn.execute("SELECT COALESCE(lang,'text'), COUNT(*) FROM files GROUP BY 1 ORDER BY 2 DESC").fetchall()
        size = store.db_path(self.root).stat().st_size
        when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(meta.get("indexed_at", "0") or 0)))
        lang_s = ", ".join(f"{a} {b}" for a, b in langs)
        return (f"{self.root}: {nfiles} files, {nchunks} chunks, {size / 1e6:.1f} MB, "
                f"indexed {when}, complete={meta.get('complete', '?')}. Languages: {lang_s}")
