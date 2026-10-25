"""Full and incremental indexing."""

from __future__ import annotations

import hashlib
import os
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from . import store
from .chunker import chunk_file
from .imports import extract_imports
from .tokenize import path_terms, text_terms
from .walker import SNIFF_BYTES, list_files, looks_binary_or_minified

BATCH_FILES = 200
SKIPPED_LANG = "(skipped)"
MAX_STORED_LINE = 500


@dataclass
class IndexStats:
    root: str
    files_total: int = 0
    files_indexed: int = 0
    files_unchanged: int = 0
    files_removed: int = 0
    files_skipped: int = 0
    chunks_total: int = 0
    seconds: float = 0.0
    complete: bool = True
    rebuilt: bool = False
    busy: bool = False
    languages: Counter = field(default_factory=Counter)

    def summary(self) -> str:
        if self.busy:
            return "thrift: another process is indexing this project; try again shortly."
        langs = ", ".join(f"{k} {v}" for k, v in self.languages.most_common(6)) or "none"
        state = "" if self.complete else " (partial: time budget hit; re-run to finish)"
        return (
            f"thrift index{' rebuilt' if self.rebuilt else ''}: {self.files_total} files "
            f"({self.files_indexed} indexed, {self.files_unchanged} unchanged, "
            f"{self.files_removed} removed, {self.files_skipped} skipped as binary/minified), {self.chunks_total} chunks in {self.seconds:.1f}s"
            f"{state}. Languages: {langs}."
        )


def _insert_file(conn, rel: str, info, data: bytes, sha1: str) -> int:
    lang, chunks = chunk_file(rel, data)
    pterms = path_terms(rel)
    text = data.decode("utf-8", "replace")
    cur = conn.cursor()
    for c in chunks:
        if len(c.text) > MAX_STORED_LINE and any(len(ln) > MAX_STORED_LINE for ln in c.text.split("\n")):
            c.text = "\n".join(
                ln if len(ln) <= MAX_STORED_LINE else ln[:MAX_STORED_LINE] + " …"
                for ln in c.text.split("\n")
            )
        cur.execute(
            "INSERT INTO chunks(path, start_line, end_line, kind, symbol, qualname, signature, text)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (rel, c.start_line, c.end_line, c.kind, c.symbol, c.qualname, c.signature, c.text),
        )
        sym_src = " ".join(x for x in (c.symbol, (c.qualname or "").replace(".", " ")) if x)
        cur.execute(
            "INSERT INTO fts(rowid, symbol_terms, path_terms, sig_terms, body_terms) VALUES (?,?,?,?,?)",
            (
                cur.lastrowid,
                text_terms(sym_src),
                pterms,
                text_terms((c.signature or "") + " " + c.extra_terms),
                text_terms(c.text),
            ),
        )
    for spec in extract_imports(rel, lang, text):
        cur.execute("INSERT INTO imports(path, spec) VALUES (?, ?)", (rel, spec))
    cur.execute(
        "INSERT OR REPLACE INTO files(path, mtime_ns, size, sha1, lang, nchunks) VALUES (?,?,?,?,?,?)",
        (rel, info.mtime_ns, info.size, sha1, lang, len(chunks)),
    )
    return len(chunks)


def _delete_file(conn, rel: str) -> None:
    ids = [r[0] for r in conn.execute("SELECT id FROM chunks WHERE path = ?", (rel,))]
    if ids:
        conn.executemany("DELETE FROM fts WHERE rowid = ?", [(i,) for i in ids])
        conn.execute("DELETE FROM chunks WHERE path = ?", (rel,))
    conn.execute("DELETE FROM imports WHERE path = ?", (rel,))
    conn.execute("DELETE FROM files WHERE path = ?", (rel,))


def index_project(
    root: str | os.PathLike,
    full: bool = False,
    time_budget: float | None = None,
    lock_wait: float = 0.0,
    progress=None,
) -> IndexStats:
    """Index (or incrementally refresh) ``root``.

    ``time_budget`` (seconds) stops early with a partial-but-valid index; the
    next run continues where it left off. Returns ``stats.busy`` if another
    writer holds the lock for longer than ``lock_wait``.
    """
    root = Path(root).resolve()
    t0 = time.monotonic()
    stats = IndexStats(root=str(root))
    try:
        with store.writer_lock(root, wait_s=lock_wait):
            conn, fresh = store.open_writable(root, rebuild=full)
            stats.rebuilt = fresh
            try:
                _index_locked(conn, root, stats, t0, time_budget, progress)
            finally:
                conn.close()
    except store.LockBusy:
        stats.busy = True
    stats.seconds = time.monotonic() - t0
    return stats


def _index_locked(conn, root: Path, stats: IndexStats, t0: float, time_budget, progress) -> None:
    files = list_files(root)
    stats.files_total = len(files)
    current = {f.rel: f for f in files}
    known = {r["path"]: r for r in conn.execute("SELECT path, mtime_ns, size, sha1 FROM files")}

    removed = [p for p in known if p not in current]
    with conn:
        for p in removed:
            _delete_file(conn, p)
    stats.files_removed = len(removed)

    todo = []
    for f in files:
        k = known.get(f.rel)
        if k is not None and k["mtime_ns"] == f.mtime_ns and k["size"] == f.size:
            stats.files_unchanged += 1
        else:
            todo.append(f)

    for i in range(0, len(todo), BATCH_FILES):
        if time_budget is not None and time.monotonic() - t0 > time_budget:
            stats.complete = False
            break
        batch = todo[i : i + BATCH_FILES]
        with conn:
            for f in batch:
                try:
                    data = (root / f.rel).read_bytes()
                except OSError:
                    continue
                sha1 = hashlib.sha1(data).hexdigest()
                if looks_binary_or_minified(data[:SNIFF_BYTES], f.rel):
                    if f.rel in known:
                        _delete_file(conn, f.rel)
                    # remember it so unchanged skipped files aren't re-read next time
                    conn.execute(
                        "INSERT OR REPLACE INTO files(path, mtime_ns, size, sha1, lang, nchunks)"
                        " VALUES (?,?,?,?,?,0)",
                        (f.rel, f.mtime_ns, f.size, sha1, SKIPPED_LANG),
                    )
                    stats.files_skipped += 1
                    continue
                k = known.get(f.rel)
                if k is not None and k["sha1"] == sha1:
                    conn.execute(
                        "UPDATE files SET mtime_ns = ?, size = ? WHERE path = ?", (f.mtime_ns, f.size, f.rel)
                    )
                    stats.files_unchanged += 1
                    continue
                if k is not None:
                    _delete_file(conn, f.rel)
                _insert_file(conn, f.rel, f, data, sha1)
                stats.files_indexed += 1
        if progress:
            progress(min(i + BATCH_FILES, len(todo)), len(todo))

    with conn:
        store.set_meta(conn, root=str(root), indexed_at=int(time.time()), complete=int(stats.complete))
    stats.chunks_total = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    with conn:
        store.set_meta(conn, chunks=stats.chunks_total)
    for lang, n in conn.execute("SELECT lang, COUNT(*) FROM files WHERE lang IS NOT ? GROUP BY lang", (SKIPPED_LANG,)):
        stats.languages[lang or "text"] = n
    stats.files_skipped = conn.execute("SELECT COUNT(*) FROM files WHERE lang = ?", (SKIPPED_LANG,)).fetchone()[0]


def needs_index(root: str | os.PathLike) -> bool:
    return not store.db_path(root).is_file()
