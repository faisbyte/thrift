"""Minify JSON tool results: drop insignificant whitespace, nothing else."""

from __future__ import annotations

import json

from .base import Result, Transform, register


class Num(str):
    """A JSON number kept as its original source literal (1.0 stays 1.0)."""


class Obj(list):
    """A JSON object as its ordered (key, value) pairs, duplicates included."""


def parse_exact(text: str):
    """Parse JSON without losing anything: number literals (Num), key order and
    duplicate keys (Obj) all survive."""
    return json.loads(text, parse_float=Num, parse_int=Num, object_pairs_hook=Obj)


def parse_container(text: str):
    """parse_exact(text) if it is a JSON object or array, else None."""
    if text.strip()[:1] not in ("{", "["):
        return None
    try:
        return parse_exact(text)
    except ValueError:
        return None


def json_tool_result(block, label):
    """(parse_exact tree, minified text) for a tool result the classifier
    labelled JSON, else None.

    Declined: other tool_result kinds (file reads, edit confirmations, ...),
    since later Edit calls depend on their exact bytes; content with non-text
    parts (images), which a string replacement would drop; and text holding a
    lone surrogate escape, which unescaped is not encodable as UTF-8.
    """
    if block.type != "tool_result" or label.kind != "tool_result:json":
        return None
    content = block.raw.get("content") if isinstance(block.raw, dict) else None
    if isinstance(content, list) and any(not isinstance(c, dict) or c.get("type") != "text" for c in content):
        return None
    tree = parse_container(block.text)
    if tree is None:
        return None
    minified = dumps_exact(tree)
    try:
        minified.encode("utf-8")
    except UnicodeEncodeError:
        return None
    return tree, minified


def is_records(v) -> bool:
    """In a parse_exact tree: a list of >=2 objects sharing one non-empty,
    duplicate-free key set, every value primitive (not an object or array)."""
    if isinstance(v, Obj) or not isinstance(v, list) or len(v) < 2:
        return False
    if not all(isinstance(r, Obj) for r in v):
        return False
    keys = {k for k, _ in v[0]}
    return bool(keys) and all(
        len(r) == len(keys) and {k for k, _ in r} == keys and not any(isinstance(x, list) for _, x in r)
        for r in v  # Obj is a list subclass, so isinstance(x, list) covers objects and arrays
    )


def dumps_exact(obj) -> str:
    """json.dumps(obj, separators=(",", ":"), ensure_ascii=False), but over a
    parse_exact tree, so number literals and duplicate keys are emitted verbatim."""
    if isinstance(obj, Num):
        return str(obj)
    if isinstance(obj, Obj):
        return "{" + ",".join(f"{_s(k)}:{dumps_exact(v)}" for k, v in obj) + "}"
    if isinstance(obj, list):
        return "[" + ",".join(dumps_exact(v) for v in obj) + "]"
    return _s(obj)


def _s(v) -> str:
    return json.dumps(v, ensure_ascii=False)


class JsonMinify(Transform):
    name = "json_minify"
    lossless = True

    def applicable(self, block, label, ctx):
        return json_tool_result(block, label) is not None

    def apply(self, block, label, ctx):
        _, out = json_tool_result(block, label)
        if len(out) >= len(block.text):
            return None
        return Result(content=out, lossless=True)


register(JsonMinify())
