"""Render a JSON tool result that is a top-level list of flat, uniform records
as CSV: a header row (first record's key order), minimal quoting, "\\n" line
endings, no trailing newline.

Lossless only when every value is a JSON string and every record lists its
keys in header order; otherwise numbers/booleans/null are written as their
JSON literals (1.50, true, null) and the result is marked lossy, because CSV
cannot tell 1 from "1".
"""

from __future__ import annotations

import csv
import io

from .base import Result, Transform, register
from .json_minify import Num, dumps_exact, is_records, json_tool_result


def is_plain_str(v) -> bool:
    return isinstance(v, str) and not isinstance(v, Num)


def cell(v) -> str:
    """A string as itself; numbers (original literal), booleans and null as JSON."""
    return str(v) if isinstance(v, str) else dumps_exact(v)


class CsvTable(Transform):
    name = "csv_table"
    lossless = False  # per Result: True only for all-string, header-ordered records

    def applicable(self, block, label, ctx):
        parsed = json_tool_result(block, label)
        return parsed is not None and is_records(parsed[0])

    def apply(self, block, label, ctx):
        records, minified = json_tool_result(block, label)
        header = [k for k, _ in records[0]]
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        w.writerow(header)
        for r in records:
            row = dict(r)  # is_records guarantees unique keys
            w.writerow([cell(row[k]) for k in header])
        out = buf.getvalue().removesuffix("\n")
        if len(out) >= len(minified):  # contract guard; not expected to fire for >=2 records
            return None
        lossless = all([k for k, _ in r] == header and all(is_plain_str(v) for _, v in r) for r in records)
        return Result(content=out, lossless=lossless)


register(CsvTable())
