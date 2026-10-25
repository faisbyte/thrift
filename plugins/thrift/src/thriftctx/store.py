"""SQLite storage for the index.

STDLIB ONLY: the hook opens the index read-only through this module.

Layout: ``<project>/.thrift/index.db`` (WAL mode) plus ``index.lock`` for the
single-writer lock and a ``.gitignore`` containing ``*``.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import time
from pathlib import Path

from .tokenize import TOKENIZER_VERSION

SCHEMA_VERSION = "2"
INDEX_DIRNAME = ".thrift"
DB_NAME = "index.db"
LOCK_NAME = "index.lock"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS files(
    path TEXT PRIMARY KEY,
    mtime_ns INTEGER NOT NULL,
    size INTEGER NOT NULL,
    sha1 TEXT NOT NULL,
    lang TEXT,
    nchunks INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS chunks(
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL,
    start_line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    kind TEXT NOT NULL,
    symbol TEXT,
    qualname TEXT,
    signature TEXT,
    text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS chunks_path ON chunks(path);
CREATE INDEX IF NOT EXISTS chunks_symbol ON chunks(symbol COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS chunks_qualname ON chunks(qualname COLLATE NOCASE);
CREATE TABLE IF NOT EXISTS imports(path TEXT NOT NULL, spec TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS imports_path ON imports(path);
CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(
    symbol_terms, path_terms, sig_terms, body_terms,
    tokenize = "unicode61 tokenchars '_'",
    prefix = '3'
);
CREATE VIRTUAL TABLE IF NOT EXISTS fts_vocab USING fts5vocab(fts, 'row');
"""


def index_dir(root: str | os.PathLike) -> Path:
    return Path(root) / INDEX_DIRNAME


def db_path(root: str | os.PathLike) -> Path:
    return index_dir(root) / DB_NAME


def find_project_root(start: str | os.PathLike | None = None) -> Path | None:
    """Walk up from ``start`` to the first directory that has an index."""
    p = Path(start or os.getcwd()).resolve()
    for d in (p, *p.parents):
        if (d / INDEX_DIRNAME / DB_NAME).is_file():
            return d
    return None


def open_readonly(root: str | os.PathLike, busy_ms: int = 50) -> sqlite3.Connection:
    """Read-only connection; raises sqlite3.Error / FileNotFoundError."""
    path = db_path(root)
    if not path.is_file():
        raise FileNotFoundError(str(path))
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=busy_ms / 1000)
    conn.row_factory = sqlite3.Row
    return conn


def _create(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    conn.executemany(
        "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
        [
            ("schema_version", SCHEMA_VERSION),
            ("tokenizer_version", TOKENIZER_VERSION),
            ("created_at", str(int(time.time()))),
        ],
    )
    conn.commit()


def _versions_ok(conn: sqlite3.Connection) -> bool:
    try:
        rows = dict(conn.execute("SELECT key, value FROM meta").fetchall())
    except sqlite3.Error:
        return False
    return (
        rows.get("schema_version") == SCHEMA_VERSION
        and rows.get("tokenizer_version") == TOKENIZER_VERSION
    )


def open_writable(root: str | os.PathLike, rebuild: bool = False) -> tuple[sqlite3.Connection, bool]:
    """Open (creating if needed) the index for writing.

    Returns ``(conn, fresh)`` where ``fresh`` means the DB was just created
    (empty). It is recreated from scratch when ``rebuild`` is set or the
    schema/tokenizer version changed. The caller must hold
    :func:`writer_lock`.
    """
    d = index_dir(root)
    d.mkdir(parents=True, exist_ok=True)
    gi = d / ".gitignore"
    if not gi.exists():
        gi.write_text("*\n")
    path = d / DB_NAME
    if path.exists() and not rebuild:
        conn = sqlite3.connect(path, timeout=5)
        if not _versions_ok(conn):
            conn.close()
            rebuild = True
        else:
            conn.row_factory = sqlite3.Row
            return conn, False
    if rebuild:
        for suffix in ("", "-wal", "-shm"):
            with contextlib.suppress(FileNotFoundError):
                os.remove(str(path) + suffix)
    conn = sqlite3.connect(path, timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    _create(conn)
    conn.row_factory = sqlite3.Row
    return conn, True


def set_meta(conn: sqlite3.Connection, **values: object) -> None:
    conn.executemany(
        "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
        [(k, str(v)) for k, v in values.items()],
    )


def get_meta(conn: sqlite3.Connection) -> dict[str, str]:
    return dict(conn.execute("SELECT key, value FROM meta").fetchall())


class LockBusy(Exception):
    pass


@contextlib.contextmanager
def writer_lock(root: str | os.PathLike, wait_s: float = 0.0):
    """Exclusive inter-process writer lock (``.thrift/index.lock``).

    Raises :class:`LockBusy` if another writer holds it after ``wait_s``.
    On platforms without ``fcntl`` the lock is a no-op.
    """
    d = index_dir(root)
    d.mkdir(parents=True, exist_ok=True)
    try:
        import fcntl
    except ImportError:  # pragma: no cover - Windows
        yield
        return
    fd = os.open(d / LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o644)
    deadline = time.monotonic() + wait_s
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise LockBusy(str(d / LOCK_NAME)) from None
                time.sleep(0.05)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
