"""Split source files into retrievable chunks.

Symbol-aware chunking via tree-sitter for Python, JS/TS(X), Rust, Java, C and
C++; markdown by heading; everything else in overlapping line windows.

Chunk rules:
- functions / methods / types each get a chunk, including decorators and
  directly preceding doc comments;
- a class-like container with methods gets a *header* chunk (declaration up
  to its first member) spanning the full class range, plus one chunk per
  member; member names are added as extra search terms on the header;
- definitions longer than ``MAX_DEF_LINES`` are split into windows that keep
  the symbol name;
- top-level code outside any definition is grouped into ``module`` blocks.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

MAX_DEF_LINES = 150
SPLIT_WINDOW = 100
SPLIT_OVERLAP = 10
HEADER_MAX_LINES = 40
MODULE_BLOCK_LINES = 60
WINDOW_LINES = 60
WINDOW_OVERLAP = 10
SIGNATURE_MAX = 120

LANG_BY_EXT = {
    "py": "python", "pyi": "python",
    "js": "javascript", "mjs": "javascript", "cjs": "javascript", "jsx": "javascript",
    "ts": "typescript", "mts": "typescript", "cts": "typescript",
    "tsx": "tsx",
    "rs": "rust",
    "java": "java",
    "c": "c",
    "h": "cpp", "hpp": "cpp", "hh": "cpp", "hxx": "cpp",
    "cc": "cpp", "cpp": "cpp", "cxx": "cpp", "c++": "cpp",
    "md": "markdown", "markdown": "markdown", "mdx": "markdown",
}


@dataclass
class Chunk:
    start_line: int  # 1-based, inclusive
    end_line: int  # 1-based, inclusive
    kind: str
    symbol: str | None
    qualname: str | None
    signature: str | None
    text: str
    extra_terms: str = ""  # e.g. member names for class headers


def detect_language(path: str) -> str | None:
    name = path.rsplit("/", 1)[-1].lower()
    if "." not in name:
        return None
    return LANG_BY_EXT.get(name.rsplit(".", 1)[1])


# --------------------------------------------------------------------------
# tree-sitter setup

_PARSERS: dict = {}


def _load_language(lang: str):
    import tree_sitter as ts

    if lang == "python":
        import tree_sitter_python as m

        return ts.Language(m.language())
    if lang == "javascript":
        import tree_sitter_javascript as m

        return ts.Language(m.language())
    if lang == "typescript":
        import tree_sitter_typescript as m

        return ts.Language(m.language_typescript())
    if lang == "tsx":
        import tree_sitter_typescript as m

        return ts.Language(m.language_tsx())
    if lang == "rust":
        import tree_sitter_rust as m

        return ts.Language(m.language())
    if lang == "java":
        import tree_sitter_java as m

        return ts.Language(m.language())
    if lang == "c":
        import tree_sitter_c as m

        return ts.Language(m.language())
    if lang == "cpp":
        import tree_sitter_cpp as m

        return ts.Language(m.language())
    return None


def _parser(lang: str):
    if lang not in _PARSERS:
        try:
            import tree_sitter as ts

            language = _load_language(lang)
            _PARSERS[lang] = ts.Parser(language) if language is not None else None
        except Exception:
            _PARSERS[lang] = None
    return _PARSERS[lang]


# --------------------------------------------------------------------------
# language specs

_COMMENT_TYPES = frozenset({"comment", "line_comment", "block_comment"})


@dataclass(frozen=True)
class Spec:
    defs: dict  # node type -> kind
    containers: frozenset  # def types whose body holds member defs
    wrappers: frozenset  # wrap a def; outer range is used
    transparent: frozenset  # descend (namespaces, modules), qualifying names
    body_passthrough: frozenset = frozenset()  # nested body lists inside containers
    var_decls: frozenset = frozenset()  # const f = () => {}
    field_decls: frozenset = frozenset()  # class field holding a function
    func_values: frozenset = frozenset()


_JS_DEFS = {
    "function_declaration": "function",
    "generator_function_declaration": "function",
    "class_declaration": "class",
    "abstract_class_declaration": "class",
    "class": "class",
    "method_definition": "method",
    "interface_declaration": "interface",
    "type_alias_declaration": "type",
    "enum_declaration": "enum",
}
_JS_SPEC = Spec(
    defs=_JS_DEFS,
    containers=frozenset({"class_declaration", "abstract_class_declaration", "class"}),
    wrappers=frozenset({"export_statement"}),
    transparent=frozenset({"internal_module", "module", "expression_statement"}),
    var_decls=frozenset({"lexical_declaration", "variable_declaration"}),
    field_decls=frozenset({"public_field_definition", "field_definition"}),
    func_values=frozenset({"arrow_function", "function_expression", "function", "generator_function"}),
)

SPECS: dict[str, Spec] = {
    "python": Spec(
        defs={"function_definition": "function", "class_definition": "class"},
        containers=frozenset({"class_definition"}),
        wrappers=frozenset({"decorated_definition"}),
        transparent=frozenset(),
    ),
    "javascript": _JS_SPEC,
    "typescript": _JS_SPEC,
    "tsx": _JS_SPEC,
    "rust": Spec(
        defs={
            "function_item": "function",
            "struct_item": "struct",
            "enum_item": "enum",
            "union_item": "struct",
            "trait_item": "trait",
            "impl_item": "impl",
            "macro_definition": "macro",
            "type_item": "type",
        },
        containers=frozenset({"trait_item", "impl_item"}),
        wrappers=frozenset(),
        transparent=frozenset({"mod_item"}),
        body_passthrough=frozenset({"declaration_list"}),
    ),
    "java": Spec(
        defs={
            "class_declaration": "class",
            "interface_declaration": "interface",
            "enum_declaration": "enum",
            "record_declaration": "class",
            "annotation_type_declaration": "interface",
            "method_declaration": "method",
            "constructor_declaration": "method",
        },
        containers=frozenset(
            {"class_declaration", "interface_declaration", "enum_declaration", "record_declaration"}
        ),
        wrappers=frozenset(),
        transparent=frozenset(),
        body_passthrough=frozenset({"enum_body_declarations"}),
    ),
    "c": Spec(
        defs={
            "function_definition": "function",
            "struct_specifier": "struct",
            "union_specifier": "struct",
            "enum_specifier": "enum",
            "type_definition": "type",
        },
        containers=frozenset(),
        wrappers=frozenset(),
        transparent=frozenset({"linkage_specification", "declaration_list", "preproc_ifdef", "preproc_if"}),
    ),
    "cpp": Spec(
        defs={
            "function_definition": "function",
            "class_specifier": "class",
            "struct_specifier": "struct",
            "union_specifier": "struct",
            "enum_specifier": "enum",
            "type_definition": "type",
        },
        containers=frozenset({"class_specifier", "struct_specifier"}),
        wrappers=frozenset({"template_declaration"}),
        transparent=frozenset(
            {"namespace_definition", "linkage_specification", "declaration_list", "preproc_ifdef", "preproc_if"}
        ),
    ),
}

_C_NAME_TYPES = frozenset(
    {"identifier", "field_identifier", "qualified_identifier", "destructor_name", "operator_name",
     "template_function", "type_identifier"}
)


# --------------------------------------------------------------------------
# extraction


class _Ctx:
    def __init__(self, src: bytes, lines: list[str], lang: str, spec: Spec):
        self.src = src
        self.lines = lines
        self.lang = lang
        self.spec = spec
        self.chunks: list[Chunk] = []
        self.covered: list[tuple[int, int]] = []  # top-level covered rows (0-based)

    def text(self, node) -> str:
        return self.src[node.start_byte : node.end_byte].decode("utf-8", "replace")

    def line_text(self, start_row: int, end_row: int) -> str:
        return "\n".join(self.lines[start_row : end_row + 1])


def _norm_ws(s: str) -> str:
    s = re.sub(r"\s+", " ", s).strip()
    return re.sub(r"([(\[<]) ", r"\1", re.sub(r",? ([)\]>])", r"\1", s))


def _signature(ctx: _Ctx, node) -> str:
    body = node.child_by_field_name("body")
    if body is not None and body.start_byte > node.start_byte:
        sig = ctx.src[node.start_byte : body.start_byte].decode("utf-8", "replace")
    else:
        sig = ctx.lines[node.start_point.row] if node.start_point.row < len(ctx.lines) else ""
    sig = _norm_ws(sig).rstrip("{:").rstrip()
    return sig[:SIGNATURE_MAX]


def _c_declarator_name(ctx: _Ctx, node) -> str | None:
    d = node.child_by_field_name("declarator")
    for _ in range(8):
        if d is None:
            return None
        if d.type in _C_NAME_TYPES:
            return ctx.text(d)
        nxt = d.child_by_field_name("declarator")
        if nxt is None:
            for c in d.named_children:
                if c.type in _C_NAME_TYPES:
                    return ctx.text(c)
            return None
        d = nxt
    return None


def _def_name(ctx: _Ctx, node) -> str | None:
    t = node.type
    if t == "impl_item":
        ty = node.child_by_field_name("type")
        return re.split(r"[<\s]", ctx.text(ty), 1)[0] if ty is not None else None
    if t == "function_definition" and ctx.lang in ("c", "cpp"):
        return _c_declarator_name(ctx, node)
    if t == "type_definition":
        d = node.child_by_field_name("declarator")
        return ctx.text(d) if d is not None else None
    name = node.child_by_field_name("name")
    if name is not None:
        return ctx.text(name)
    return None


def _split_qualified(name: str) -> tuple[str, str | None]:
    """'Foo::bar' -> ('bar', 'Foo'); 'bar' -> ('bar', None)."""
    if "::" in name:
        scope, _, last = name.rpartition("::")
        return last, scope.replace("::", ".")
    return name, None


def _leading_comment_start(node) -> int:
    """Row where directly-preceding doc comments begin (or node's own row)."""
    start = node.start_point.row
    prev = node.prev_named_sibling
    while prev is not None and prev.type in _COMMENT_TYPES and prev.end_point.row >= start - 1:
        start = prev.start_point.row
        prev = prev.prev_named_sibling
    return start


def _func_value_name(ctx: _Ctx, node) -> tuple[str, object] | None:
    """For `const f = () => {}` style declarations return (name, value node)."""
    spec = ctx.spec
    if node.type in spec.var_decls:
        for d in node.named_children:
            if d.type == "variable_declarator":
                v = d.child_by_field_name("value")
                n = d.child_by_field_name("name")
                if v is not None and n is not None and v.type in spec.func_values:
                    return ctx.text(n), v
    elif node.type in spec.field_decls:
        v = node.child_by_field_name("value")
        n = node.child_by_field_name("name") or node.child_by_field_name("property")
        if v is not None and n is not None and v.type in spec.func_values:
            return ctx.text(n), v
    return None


def _unwrap(ctx: _Ctx, node):
    """Return the def node inside a wrapper (decorator/export/template), or None."""
    for c in node.named_children:
        if c.type in ctx.spec.defs or _func_value_name(ctx, c) or c.type in ctx.spec.wrappers:
            if c.type in ctx.spec.wrappers:
                return _unwrap(ctx, c)
            return c
    return None


def _member_nodes(ctx: _Ctx, container) -> list:
    body = container.child_by_field_name("body")
    if body is None:
        return []
    out = []
    stack = list(body.named_children)
    while stack:
        c = stack.pop(0)
        if c.type in ctx.spec.body_passthrough:
            stack[0:0] = list(c.named_children)
            continue
        if c.type in ctx.spec.wrappers:
            inner = _unwrap(ctx, c)
            if inner is not None:
                out.append((c, inner))
            continue
        if c.type in ctx.spec.defs or _func_value_name(ctx, c):
            # skip bodiless methods (interface/trait signatures, abstract decls)
            if ctx.spec.defs.get(c.type) in ("function", "method") and c.child_by_field_name("body") is None:
                continue
            out.append((c, c))
    return out


def _emit(ctx: _Ctx, start_row: int, end_row: int, kind: str, symbol, qualname, signature,
          text: str | None = None, extra_terms: str = "") -> None:
    if text is None:
        text = ctx.line_text(start_row, end_row)
    nlines = end_row - start_row + 1
    if nlines > MAX_DEF_LINES and text.count("\n") + 1 == nlines:
        s = start_row
        while s <= end_row:
            e = min(s + SPLIT_WINDOW - 1, end_row)
            ctx.chunks.append(
                Chunk(s + 1, e + 1, kind, symbol, qualname, signature, ctx.line_text(s, e), extra_terms)
            )
            if e == end_row:
                break
            s = e + 1 - SPLIT_OVERLAP
        return
    ctx.chunks.append(Chunk(start_row + 1, end_row + 1, kind, symbol, qualname, signature, text, extra_terms))


def _handle_def(ctx: _Ctx, outer, node, prefix: str | None, in_container: bool) -> bool:
    """Emit chunks for a definition. Returns True if something was emitted."""
    spec = ctx.spec
    fv = _func_value_name(ctx, node)
    if fv is not None:
        name, value = fv
        kind = "method" if in_container else "function"
        symbol = name
        qual = f"{prefix}.{name}" if prefix else name
        start = _leading_comment_start(outer)
        sig = _norm_ws(ctx.src[node.start_byte : (value.child_by_field_name("body") or value).start_byte]
                       .decode("utf-8", "replace")).rstrip("{=> ").rstrip()[:SIGNATURE_MAX]
        _emit(ctx, start, outer.end_point.row, kind, symbol, qual, sig)
        return True

    kind = spec.defs.get(node.type)
    if kind is None:
        return False
    raw = _def_name(ctx, node)
    if node.type in ("struct_specifier", "union_specifier", "enum_specifier", "class_specifier") and (
        raw is None or node.child_by_field_name("body") is None
    ):
        return False  # anonymous, forward declaration or plain type use
    symbol, scope = _split_qualified(raw) if raw else (None, None)
    if kind == "function" and (in_container or scope):
        kind = "method"
    qual_prefix = prefix
    if scope:
        qual_prefix = f"{prefix}.{scope}" if prefix else scope
    qual = f"{qual_prefix}.{symbol}" if (qual_prefix and symbol) else symbol
    start = _leading_comment_start(outer)
    end = outer.end_point.row
    sig = _signature(ctx, node)

    members = _member_nodes(ctx, node) if node.type in spec.containers else []
    if members:
        first_member_row = min(_leading_comment_start(o) for o, _ in members)
        header_end = max(start, min(first_member_row - 1, start + HEADER_MAX_LINES - 1))
        member_names = []
        for _, m in members:
            fvm = _func_value_name(ctx, m)
            nm = fvm[0] if fvm else _def_name(ctx, m)
            if nm:
                member_names.append(_split_qualified(nm)[0])
        _emit(ctx, start, end, kind, symbol, qual, sig,
              text=ctx.line_text(start, header_end), extra_terms=" ".join(member_names))
        for o, m in members:
            _handle_def(ctx, o, m, qual, True)
    else:
        _emit(ctx, start, end, kind, symbol, qual, sig)
    return True


def _walk_top(ctx: _Ctx, nodes, prefix: str | None) -> None:
    spec = ctx.spec
    for node in nodes:
        t = node.type
        if t in spec.wrappers:
            inner = _unwrap(ctx, node)
            if inner is not None and _handle_def(ctx, node, inner, prefix, False):
                ctx.covered.append((_leading_comment_start(node), node.end_point.row))
            continue
        if t in spec.transparent:
            name_node = node.child_by_field_name("name")
            sub = prefix
            if name_node is not None and t != "expression_statement":
                nm = ctx.text(name_node)
                sub = f"{prefix}.{nm}" if prefix else nm
            body = node.child_by_field_name("body")
            children = body.named_children if body is not None else node.named_children
            _walk_top(ctx, children, sub)
            continue
        if t in spec.defs or _func_value_name(ctx, node):
            if _handle_def(ctx, node, node, prefix, False):
                ctx.covered.append((_leading_comment_start(node), node.end_point.row))


def _module_blocks(ctx: _Ctx) -> None:
    n = len(ctx.lines)
    covered = [False] * n
    for s, e in ctx.covered:
        for i in range(max(s, 0), min(e, n - 1) + 1):
            covered[i] = True
    i = 0
    while i < n:
        if covered[i] or not ctx.lines[i].strip():
            i += 1
            continue
        j = i
        while j + 1 < n and not covered[j + 1] and (j + 1 - i) < MODULE_BLOCK_LINES:
            j += 1
        # trim trailing blank lines
        while j > i and not ctx.lines[j].strip():
            j -= 1
        body = ctx.line_text(i, j)
        if re.search(r"[A-Za-z0-9]", body):  # skip lone braces / punctuation
            ctx.chunks.append(Chunk(i + 1, j + 1, "module", None, None, None, body))
        i = j + 1


def _chunk_tree(path: str, src: bytes, text: str, lang: str) -> list[Chunk] | None:
    parser = _parser(lang)
    spec = SPECS.get(lang)
    if parser is None or spec is None:
        return None
    try:
        tree = parser.parse(src)
    except Exception:
        return None
    lines = text.split("\n")
    ctx = _Ctx(src, lines, lang, spec)
    _walk_top(ctx, tree.root_node.named_children, None)
    _module_blocks(ctx)
    ctx.chunks.sort(key=lambda c: (c.start_line, -c.end_line))
    return ctx.chunks


# --------------------------------------------------------------------------
# fallbacks


def chunk_windows(lines: list[str], size: int = WINDOW_LINES, overlap: int = WINDOW_OVERLAP,
                  offset: int = 0, kind: str = "window", symbol: str | None = None) -> list[Chunk]:
    out: list[Chunk] = []
    n = len(lines)
    if n == 0:
        return out
    s = 0
    while s < n:
        e = min(s + size, n)
        body = "\n".join(lines[s:e])
        if body.strip():
            out.append(Chunk(offset + s + 1, offset + e, kind, symbol, symbol, None, body))
        if e >= n:
            break
        s = e - overlap
    return out


_MD_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")


def chunk_markdown(lines: list[str]) -> list[Chunk]:
    sections: list[tuple[int, str | None]] = [(0, None)]
    fence = False
    for i, ln in enumerate(lines):
        if ln.lstrip().startswith(("```", "~~~")):
            fence = not fence
            continue
        if not fence:
            m = _MD_HEADING.match(ln)
            if m and i > 0:
                sections.append((i, m.group(2)))
            elif m and i == 0:
                sections[0] = (0, m.group(2))
    out: list[Chunk] = []
    for idx, (start, title) in enumerate(sections):
        end = sections[idx + 1][0] if idx + 1 < len(sections) else len(lines)
        seg = lines[start:end]
        while seg and not seg[-1].strip():
            seg.pop()
            end -= 1
        if not any(s.strip() for s in seg):
            continue
        if len(seg) > 80:
            out.extend(chunk_windows(seg, 60, 10, offset=start, kind="section", symbol=title))
        else:
            out.append(Chunk(start + 1, start + len(seg), "section", title, title, None, "\n".join(seg)))
    return out


def chunk_file(path: str, data: bytes) -> tuple[str | None, list[Chunk]]:
    """Chunk one file. Returns ``(language, chunks)``."""
    text = data.decode("utf-8", "replace").replace("\r\n", "\n")
    lang = detect_language(path)
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()  # trailing newline is not an extra line
    if lang == "markdown":
        return lang, chunk_markdown(lines)
    if lang is not None:
        chunks = _chunk_tree(path, text.encode("utf-8"), text, lang)
        if chunks is not None:
            return lang, chunks
    return lang, chunk_windows(lines)
