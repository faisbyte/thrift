"""Re-encode record-like JSON tool results as TOON (Token-Oriented Object
Notation): uniform arrays of objects become a header plus CSV-like rows."""

from __future__ import annotations

import json

import toon_format

from .base import Result, Transform, register
from .json_minify import Num, Obj, dumps_exact, is_records, json_tool_result


def has_records(tree) -> bool:
    """True if any array anywhere in a parse_exact tree is_records."""
    if is_records(tree):
        return True
    if isinstance(tree, Obj):
        return any(has_records(v) for _, v in tree)
    return isinstance(tree, list) and any(has_records(v) for v in tree)


class Toon(Transform):
    name = "toon"
    lossless = True

    def applicable(self, block, label, ctx):
        parsed = json_tool_result(block, label)
        return parsed is not None and has_records(parsed[0])

    def apply(self, block, label, ctx):
        _, minified = json_tool_result(block, label)
        out = toon_format.encode(json.loads(block.text), indent_size=2, delimiter=",")
        if len(out) >= len(minified):  # TOON loses to compact JSON on non-uniform data
            return None
        back = toon_format.decode(out, strict=True, parse_float=Num, parse_int=Num, object_pairs_hook=Obj)
        if dumps_exact(back) != minified:  # byte-exact round trip: literals, types, key order
            return None
        return Result(content=out, lossless=True)


register(Toon())
