from pathlib import Path

import pytest

from thriftctx import chunker
from thriftctx.chunker import chunk_file, detect_language

FIX = Path(__file__).parent / "fixtures"


def chunks_of(rel: str):
    lang, chunks = chunk_file(rel, (FIX / rel).read_bytes())
    return lang, chunks


def by_qual(chunks):
    return {c.qualname: c for c in chunks if c.qualname}


def test_detect_language():
    assert detect_language("a/b.py") == "python"
    assert detect_language("x.tsx") == "tsx"
    assert detect_language("x.h") == "cpp"
    assert detect_language("Makefile") is None
    assert detect_language("README.md") == "markdown"


def test_python_symbols_ranges_decorators_docstrings():
    lang, chunks = chunks_of("py/shapes.py")
    assert lang == "python"
    q = by_qual(chunks)
    assert set(q) == {"make_circle", "Circle", "Circle.__init__", "Circle.area", "Circle.Builder",
                      "Circle.Builder.build"}
    mk = q["make_circle"]
    assert (mk.start_line, mk.end_line) == (8, 11)  # includes the decorator line
    assert mk.text.startswith('@register("circle")') and '"""Create a circle."""' in mk.text
    assert mk.kind == "function" and mk.signature == "def make_circle(radius)"
    area = q["Circle.area"]
    assert area.kind == "method" and area.text.lstrip().startswith("@property")
    # class header: spans the whole class, text stops before the first member
    cls = q["Circle"]
    assert (cls.start_line, cls.end_line) == (14, 28)
    assert "def __init__" not in cls.text and '"""A round shape."""' in cls.text
    assert set(cls.extra_terms.split()) == {"__init__", "area", "Builder"}
    # module-level code outside definitions
    mods = [c for c in chunks if c.kind == "module"]
    assert mods[0].start_line == 1 and "DEFAULT_SIDES" in mods[0].text
    assert any("__main__" in c.text for c in mods)


def test_typescript_arrow_functions_fields_and_types():
    lang, chunks = chunks_of("ts/service.ts")
    assert lang == "typescript"
    q = by_qual(chunks)
    assert {"fetchUser", "parseConfig", "UserService", "UserService.onChange",
            "UserService.constructor", "UserService.getUser", "User", "UserId"} <= set(q)
    assert q["fetchUser"].start_line == 3  # JSDoc comment included
    assert q["parseConfig"].kind == "function"
    assert q["UserService.onChange"].kind == "method"
    assert q["User"].kind == "interface" and q["UserId"].kind == "type"
    assert "cache" not in q  # plain fields are not chunks


def test_rust_impl_trait_and_doc_comments():
    _, chunks = chunks_of("rust/lib.rs")
    q = by_qual(chunks)
    assert {"parse", "Parser", "Parser.new", "Parser.advance", "Visitor", "Visitor.done"} <= set(q)
    assert q["parse"].start_line == 4  # `///` doc comment
    assert "Visitor.visit" not in q  # bodiless trait signature
    impl = [c for c in chunks if c.kind == "impl"]
    assert impl and impl[0].symbol == "Parser"


def test_java_class_members_and_nested():
    _, chunks = chunks_of("java/OrderService.java")
    q = by_qual(chunks)
    assert {"OrderService", "OrderService.OrderService", "OrderService.findOrder",
            "OrderService.Cache", "OrderService.Cache.evict"} <= set(q)
    assert q["OrderService.findOrder"].text.lstrip().startswith("@Override")


def test_c_functions_typedefs_enums():
    _, chunks = chunks_of("c/buffer.c")
    q = by_qual(chunks)
    assert q["buffer_append"].kind == "function"
    assert q["buffer_append"].start_line == 8  # comment above
    assert q["Buffer"].kind == "type"
    assert q["Mode"].kind == "enum"


def test_cpp_namespaces_out_of_line_methods_templates():
    _, chunks = chunks_of("cpp/widget.cpp")
    q = by_qual(chunks)
    assert {"ui.Widget", "ui.Widget.width", "ui.Widget.draw", "ui.clamp_value"} <= set(q)
    assert q["ui.Widget.draw"].kind == "method"
    assert q["ui.Widget.draw"].signature == "void Widget::draw()"
    assert q["ui.clamp_value"].text.startswith("template")
    assert not any(c.text.strip() == "}" for c in chunks)  # no lone-brace module blocks


def test_markdown_sections_ignore_fenced_headings():
    lang, chunks = chunks_of("misc/README.md")
    assert lang == "markdown"
    assert [c.symbol for c in chunks] == ["Project", "Installation", "Usage"]
    assert "make install" in chunks[1].text


def test_line_window_fallback_with_overlap():
    _, chunks = chunks_of("misc/notes.txt")
    assert [(c.start_line, c.end_line) for c in chunks] == [(1, 60), (51, 110), (101, 130)]
    assert all(c.kind == "window" for c in chunks)


def test_oversized_definition_is_split_keeping_symbol(monkeypatch):
    body = "\n".join(f"    x{i} = {i}" for i in range(400))
    src = f"def huge():\n{body}\n"
    _, chunks = chunk_file("big.py", src.encode())
    parts = [c for c in chunks if c.symbol == "huge"]
    assert len(parts) >= 4
    assert parts[0].start_line == 1 and parts[-1].end_line == 401
    assert all(p.end_line - p.start_line + 1 <= chunker.SPLIT_WINDOW for p in parts)
    assert parts[1].start_line == parts[0].end_line + 1 - chunker.SPLIT_OVERLAP


def test_unparseable_language_falls_back_to_windows(monkeypatch):
    monkeypatch.setitem(chunker._PARSERS, "python", None)
    _, chunks = chunk_file("x.py", b"def f():\n    return 1\n")
    assert [c.kind for c in chunks] == ["window"]


@pytest.mark.parametrize("rel", ["py/shapes.py", "ts/service.ts", "rust/lib.rs", "java/OrderService.java",
                                 "c/buffer.c", "cpp/widget.cpp"])
def test_chunk_text_matches_file_lines(rel):
    lines = (FIX / rel).read_text().split("\n")
    _, chunks = chunks_of(rel)
    for c in chunks:
        expected = "\n".join(lines[c.start_line - 1 : c.start_line - 1 + c.text.count("\n") + 1])
        assert c.text == expected, (rel, c.qualname)
