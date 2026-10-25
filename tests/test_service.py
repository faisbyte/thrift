"""MCP-facing operations: compact formatting, caps, ambiguity handling."""

from pathlib import Path

import pytest

from conftest import write
from thriftctx import format as fmt
from thriftctx.service import Service


@pytest.fixture
def svc(fixture_repo):
    return Service(fixture_repo)


def test_first_call_builds_index(svc, fixture_repo):
    assert not (fixture_repo / ".thrift" / "index.db").exists()
    out = svc.search("order service find order")
    assert (fixture_repo / ".thrift" / "index.db").exists()
    assert "java/OrderService.java:L" in out


def test_search_format_and_caps(svc):
    out = svc.search("circle radius area", k=3)
    lines = out.splitlines()
    assert lines[0].startswith("3 matches") or lines[0].startswith("2 matches")
    assert "py/shapes.py:L" in out
    # every snippet line is numbered and short
    for ln in lines[1:]:
        if ln.startswith("  ") and ln.strip() != "…":
            assert ":" in ln and len(ln) <= fmt.LINE_CHARS + 12
    assert len(out) < 2500


def test_search_k_is_clamped(svc):
    out = svc.search("circle", k=500)
    assert out.count(":L") <= 20


def test_search_no_terms(svc):
    assert "no searchable terms" in svc.search("the of and")


def test_get_symbol_qualified_and_plain(svc):
    out = svc.symbol("Circle.area")
    assert out.splitlines()[0].startswith("py/shapes.py:L22-24  method  Circle.area")
    assert "math.pi" in out
    assert "getUser(id" in svc.symbol("UserService.getUser")
    assert "void Widget::draw()" in svc.symbol("ui::Widget::draw")
    assert "Widget::draw" in svc.symbol("draw")


def test_get_symbol_path_disambiguation(svc):
    out = svc.symbol("rust/lib.rs:parse")
    assert out.startswith("rust/lib.rs:L")


def test_get_symbol_unknown(svc):
    assert "No definition named" in svc.symbol("does_not_exist_anywhere")


def test_get_symbol_ambiguous_lists_candidates(tmp_path):
    for i in range(5):
        write(tmp_path, f"m{i}.py", f"def run():\n    return {i}\n")
    out = Service(tmp_path).symbol("run")
    assert out.startswith('"run" is ambiguous (5 definitions)')
    assert out.count(":L1-2") == 5


def test_get_symbol_truncates_long_bodies(tmp_path):
    body = "\n".join(f"    v{i} = {i}" for i in range(300))
    write(tmp_path, "long.py", f"def long_fn():\n{body}\n")
    out = Service(tmp_path).symbol("long_fn")
    assert out.count("\n") < fmt.SYMBOL_MAX_LINES + 5
    assert "more lines: Read long.py offset=121 limit=181" in out


def test_get_symbol_big_class_shows_outline(tmp_path):
    methods = "\n".join(f"    def m{i}(self):\n" + "\n".join(f"        x = {j}" for j in range(10)) for i in range(15))
    write(tmp_path, "big.py", f'class Big:\n    """Doc."""\n\n{methods}\n')
    out = Service(tmp_path).symbol("Big")
    assert "header and members shown" in out
    assert "members (15)" in out and "def m14(self)" in out
    assert "x = 9" not in out


def test_related_files(svc):
    out = svc.related("rust/lib.rs")
    assert "imports (1): rust/util.rs" in out
    out = svc.related("rust/util.rs")
    assert "imported by (1): rust/lib.rs" in out
    out = svc.related("c/buffer.c")
    assert "imports (1): c/buffer.h" in out


def test_related_files_unknown_and_absolute(svc, fixture_repo):
    svc.search("x circle")
    assert "not in the index" in svc.related("nope/missing.py")
    out = svc.related(str(fixture_repo / "rust" / "lib.rs"))
    assert out.startswith("rust/lib.rs")


def test_refresh_picks_up_new_files(svc, fixture_repo):
    svc.search("circle")
    write(fixture_repo, "py/late.py", "def arrived_late():\n    pass\n")
    svc._last_refresh = 0  # pretend the refresh interval elapsed
    assert "arrived_late" in svc.symbol("arrived_late")
