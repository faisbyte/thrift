import os
import sqlite3
import time
from pathlib import Path

from conftest import git_init, write
from thriftctx import store
from thriftctx.imports import Resolver, extract_imports
from thriftctx.indexer import index_project
from thriftctx.walker import list_files


def rows(root, sql, args=()):
    conn = store.open_readonly(root)
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def bump(p: Path):
    st = p.stat()
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))


def test_full_then_noop(fixture_repo):
    s1 = index_project(fixture_repo)
    assert s1.rebuilt and s1.complete and s1.files_indexed >= 10 and s1.chunks_total > 30
    s2 = index_project(fixture_repo)
    assert s2.files_indexed == 0 and s2.files_unchanged == s1.files_total and not s2.rebuilt
    assert s2.chunks_total == s1.chunks_total
    assert (fixture_repo / ".thrift" / ".gitignore").read_text() == "*\n"


def test_incremental_modify_add_delete_rename(fixture_repo):
    index_project(fixture_repo)
    before = {r["path"]: r["id"] for r in rows(fixture_repo, "SELECT path, MIN(id) id FROM chunks GROUP BY path")}

    p = fixture_repo / "py" / "shapes.py"
    p.write_text(p.read_text() + "\n\ndef brand_new_function():\n    return 42\n")
    write(fixture_repo, "py/extra.py", "def added_later():\n    pass\n")
    (fixture_repo / "rust" / "util.rs").unlink()
    (fixture_repo / "c" / "buffer.c").rename(fixture_repo / "c" / "buf2.c")

    s = index_project(fixture_repo)
    assert s.files_indexed == 3  # shapes.py, extra.py, buf2.c
    assert s.files_removed == 2  # util.rs, buffer.c
    syms = {r[0] for r in rows(fixture_repo, "SELECT symbol FROM chunks")}
    assert {"brand_new_function", "added_later", "buffer_append"} <= syms
    paths = {r[0] for r in rows(fixture_repo, "SELECT path FROM files")}
    assert "rust/util.rs" not in paths and "c/buffer.c" not in paths and "c/buf2.c" in paths
    # untouched files keep their rows
    after = {r["path"]: r["id"] for r in rows(fixture_repo, "SELECT path, MIN(id) id FROM chunks GROUP BY path")}
    assert after["java/OrderService.java"] == before["java/OrderService.java"]
    # fts rows stay in sync with chunks
    n_chunks = rows(fixture_repo, "SELECT COUNT(*) FROM chunks")[0][0]
    n_fts = rows(fixture_repo, "SELECT COUNT(*) FROM fts")[0][0]
    assert n_chunks == n_fts


def test_touch_without_content_change_is_cheap(fixture_repo):
    index_project(fixture_repo)
    bump(fixture_repo / "py" / "shapes.py")
    s = index_project(fixture_repo)
    assert s.files_indexed == 0


def test_schema_version_mismatch_rebuilds(fixture_repo):
    index_project(fixture_repo)
    conn = sqlite3.connect(store.db_path(fixture_repo))
    conn.execute("UPDATE meta SET value = 'old' WHERE key = 'schema_version'")
    conn.commit()
    conn.close()
    s = index_project(fixture_repo)
    assert s.rebuilt and s.files_indexed > 0


def test_full_flag_rebuilds(fixture_repo):
    index_project(fixture_repo)
    assert index_project(fixture_repo, full=True).rebuilt


def test_lock_busy_reports_busy(fixture_repo):
    with store.writer_lock(fixture_repo):
        s = index_project(fixture_repo, lock_wait=0)
    assert s.busy


def test_time_budget_gives_partial_index_then_resumes(fixture_repo, monkeypatch):
    import thriftctx.indexer as ix

    monkeypatch.setattr(ix, "BATCH_FILES", 1)
    s = index_project(fixture_repo, time_budget=0.0)
    assert not s.complete
    s2 = index_project(fixture_repo)
    assert s2.complete and s2.files_indexed > 0


def test_walker_skips(tmp_path):
    write(tmp_path, "src/app.py", "x = 1\n")
    write(tmp_path, "package-lock.json", "{}\n")
    write(tmp_path, "node_modules/lib/index.js", "x\n")
    write(tmp_path, "img/logo.png", "not really png\n")
    write(tmp_path, "build/out.js", "x\n")
    write(tmp_path, "ignored/secret.py", "x\n")
    write(tmp_path, ".gitignore", "ignored/\n")
    (tmp_path / "empty.py").write_text("")
    rels = [f.rel for f in list_files(tmp_path)]
    assert rels == [".gitignore", "src/app.py"]


def test_walker_uses_git_when_available(tmp_path):
    git_init(tmp_path)
    write(tmp_path, ".gitignore", "*.log\nsecret/\n")
    write(tmp_path, "a.py", "x = 1\n")
    write(tmp_path, "debug.log", "x\n")
    write(tmp_path, "secret/k.py", "x\n")
    write(tmp_path, "sub/.gitignore", "local.py\n")
    write(tmp_path, "sub/local.py", "x\n")
    write(tmp_path, "sub/kept.py", "x\n")
    rels = [f.rel for f in list_files(tmp_path)]
    assert rels == [".gitignore", "a.py", "sub/.gitignore", "sub/kept.py"]


def test_binary_and_minified_are_skipped_but_long_python_lines_are_not(tmp_path):
    write(tmp_path, "data.bin.txt", "abc\x00def")
    write(tmp_path, "app.min.css", "a{}")
    write(tmp_path, "bundle.js", "var a=1;" * 2000)
    write(tmp_path, "expected.py", "def test_x():\n    expected = '" + "x" * 6000 + "'\n")
    s = index_project(tmp_path)
    syms = {r[0] for r in rows(tmp_path, "SELECT symbol FROM chunks")}
    assert "test_x" in syms
    paths = {r[0] for r in rows(tmp_path, "SELECT DISTINCT path FROM chunks")}
    assert paths == {"expected.py"}
    assert s.files_skipped == 2  # NUL file and minified bundle (min.css skipped by name)
    # stored text is clipped for very long lines
    text = rows(tmp_path, "SELECT text FROM chunks WHERE symbol = 'test_x'")[0][0]
    assert len(text) < 1000


def test_import_extraction():
    py = "import os, sys as s\nfrom .base import Shape\nfrom ..pkg import (a,\n  b)\nfrom x.y import z\n"
    assert extract_imports("p/m.py", "python", py) == [
        "os", "sys", ".base", ".base.Shape", "..pkg", "..pkg.a", "..pkg.b", "x.y", "x.y.z"]
    ts = "import { A } from './a';\nimport './side';\nconst b = require('../b');\nexport * from './c.js';"
    assert extract_imports("s/x.ts", "typescript", ts) == ["./a", "./c.js", "./side", "../b"]
    assert extract_imports("m.rs", "rust", "mod util;\nuse crate::a::b;\nuse std::io;") == ["mod:util", "crate::a::b"]


def test_import_resolution():
    files = ["src/pkg/__init__.py", "src/pkg/core.py", "src/pkg/sub/__init__.py", "src/pkg/sub/leaf.py",
             "tests/test_core.py", "utils/json.py", "web/app.ts", "web/lib/index.ts", "web/util.ts",
             "rs/src/lib.rs", "rs/src/util.rs", "rs/src/a/b.rs", "java/com/shop/model/Order.java",
             "c/buffer.c", "c/buffer.h"]
    r = Resolver(files)
    assert r.resolve("src/pkg/sub/leaf.py", "python", "..core") == ["src/pkg/core.py"]
    assert r.resolve("tests/test_core.py", "python", "pkg.core") == ["src/pkg/core.py"]
    assert r.resolve("tests/test_core.py", "python", "pkg.sub") == ["src/pkg/sub/__init__.py"]
    assert r.resolve("src/pkg/core.py", "python", "json") == []  # not utils/json.py
    assert r.resolve("web/app.ts", "typescript", "./lib") == ["web/lib/index.ts"]
    assert r.resolve("web/app.ts", "typescript", "./util.js") == ["web/util.ts"]
    assert r.resolve("web/app.ts", "typescript", "react") == []
    assert r.resolve("rs/src/lib.rs", "rust", "mod:util") == ["rs/src/util.rs"]
    assert r.resolve("rs/src/lib.rs", "rust", "crate::a::b::Thing") == ["rs/src/a/b.rs"]
    assert r.resolve("x/Svc.java", "java", "com.shop.model.Order") == ["java/com/shop/model/Order.java"]
    assert r.resolve("c/buffer.c", "c", "buffer.h") == ["c/buffer.h"]
