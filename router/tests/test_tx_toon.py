import json

import pytest
import toon_format

from reprouter.classify import Label
from reprouter.request import Block
from reprouter.transforms.base import Context, get
from reprouter.transforms.toon import Toon

JSON = Label("tool_result:json")


def tr(text):
    raw = {"type": "tool_result", "tool_use_id": "tu_1", "content": text}
    return Block(("messages", 2, "content", 0), "user", "tool_result", text, msg_index=2,
                 tool_name="mcp__gh__list_issues", tool_use_id="tu_1", raw=raw)


def run(obj_or_text, label=JSON):
    text = obj_or_text if isinstance(obj_or_text, str) else json.dumps(obj_or_text, indent=2)
    return Toon().run(tr(text), label, Context())


ISSUES = {"repo": "acme/api", "issues": [
    {"number": 101 + i, "title": f"Bug {i}", "state": "open" if i % 2 else "closed", "comments": i}
    for i in range(6)
]}


def test_encodes_uniform_records_as_toon_table():
    res = run(ISSUES)
    assert res.content.startswith("repo: acme/api\nissues[6]{number,title,state,comments}:\n")
    assert toon_format.decode(res.content) == ISSUES
    assert res.lossless is True


def test_finds_record_arrays_nested_deeper():
    obj = {"data": {"page": 1, "rows": [{"k": "a", "v": 1}, {"k": "b", "v": 2}, {"k": "c", "v": 3}]}}
    assert Toon().applicable(tr(json.dumps(obj, indent=2)), JSON, Context()) is True


@pytest.mark.parametrize("obj", [
    {"name": "x", "config": {"a": 1, "b": [1, 2, 3]}},          # no array of objects
    [1, 2, 3, 4],                                                 # scalars
    {"rows": [{"k": "a", "v": 1}]},                               # only one record
    {"rows": [{"k": "a", "v": 1}, {"k": "b", "w": 2}]},           # keys differ
    {"rows": [{"k": "a", "v": [1]}, {"k": "b", "v": [2]}]},       # non-primitive values
    {"rows": [{"k": "a"}, 7]},                                    # mixed element types
])
def test_not_applicable_unless_record_like(obj):
    assert Toon().applicable(tr(json.dumps(obj, indent=2)), JSON, Context()) is False


def rows_json(price_literal):
    rows = ",\n".join(f'  {{"id": {i}, "sku": "S-{i}", "price": {price_literal}}}' for i in range(8))
    return "[\n" + rows + "\n]"


@pytest.mark.parametrize("literal", ["1.0", "1e3", "2.50", "-0"])
def test_declines_when_toon_would_rewrite_a_number_literal(literal):
    # TOON normalises numbers (1.0 -> 1, 1e3 -> 1000); equal as floats, not byte-exact
    assert Toon().applicable(tr(rows_json(literal)), JSON, Context()) is True
    assert run(rows_json(literal)) is None


MIXED = {"pair": [{"a": 1}, {"a": 2}], "events": [
    {"type": "push", "payload": {"ref": "main", "commits": [{"sha": "abc", "files": ["a.py", "b.py"]}]}},
    {"type": "issue", "payload": {"action": "opened", "labels": [["bug"], ["p1"]]}},
    {"type": "fork", "forkee": {"id": 9, "owner": {"login": "x"}}},
]}


def test_declines_non_uniform_data_where_toon_loses_to_minified_json():
    text = json.dumps(MIXED, indent=2)
    minified = json.dumps(MIXED, separators=(",", ":"))
    assert Toon().applicable(tr(text), JSON, Context()) is True  # has one tiny record array
    assert len(toon_format.encode(MIXED)) >= len(minified)    # but TOON is bigger overall
    assert run(text) is None


def test_output_is_shorter_than_minified_json():
    res = run(ISSUES)
    assert len(res.content) < len(json.dumps(ISSUES, separators=(",", ":")))


def records_text(rows):
    return "[\n" + ",\n".join("  " + r for r in rows) + "\n]"


def test_declines_when_toon_would_reorder_keys():
    # TOON's table header follows row 1; row 2's {"b","a"} order would be lost
    rows = ['{"a": 1, "b": "xxxxxxxx"}', '{"b": "yyyyyyyy", "a": 2}'] + \
           [f'{{"a": {i}, "b": "zzzzzzzz"}}' for i in range(3, 9)]
    assert run(records_text(rows)) is None


def test_declines_on_duplicate_keys():
    rows = [f'{{"a": {i}, "b": "v", "b": "w"}}' for i in range(8)]
    assert run(records_text(rows)) is None


def test_strings_that_look_like_scalars_keep_their_type():
    obj = [{"id": "007", "flag": "true", "nil": "null", "empty": "", "comma": "a,b"} for _ in range(4)]
    res = run(obj)
    assert toon_format.decode(res.content) == obj


def test_number_literals_that_survive_are_accepted():
    res = run(rows_json("19.99"))
    assert res is not None and "19.99" in res.content


def test_deterministic():
    a, b = run(ISSUES), run(ISSUES)
    assert a.content == b.content and a.lossless == b.lossless


def test_non_tool_result_blocks_are_declined():
    user = Block(("messages", 0, "content", 0), "user", "text", json.dumps(ISSUES, indent=2), msg_index=0)
    assert Toon().run(user, Label("user_prompt"), Context()) is None


def test_tool_results_not_labelled_json_are_declined():
    assert run(ISSUES, label=Label("tool_result:file_read")) is None


def test_registered_as_lossless_toon():
    t = get("toon")
    assert isinstance(t, Toon) and t.lossless is True
