import csv
import io
import json

import pytest

from reprouter.classify import Label
from reprouter.request import Block
from reprouter.transforms.base import Context, get
from reprouter.transforms.csv_table import CsvTable

JSON = Label("tool_result:json")


def tr(text):
    raw = {"type": "tool_result", "tool_use_id": "tu_1", "content": text}
    return Block(("messages", 2, "content", 0), "user", "tool_result", text, msg_index=2,
                 tool_name="mcp__db__query", tool_use_id="tu_1", raw=raw)


def run(obj_or_text, label=JSON):
    text = obj_or_text if isinstance(obj_or_text, str) else json.dumps(obj_or_text, indent=2)
    return CsvTable().run(tr(text), label, Context())


def parse_csv(text):
    return list(csv.reader(io.StringIO(text, newline="")))


USERS = [{"login": f"user{i}", "name": f"User {i}", "role": "admin" if i == 0 else "member"} for i in range(5)]


def test_string_records_become_lossless_csv():
    res = run(USERS)
    assert res.content == ("login,name,role\nuser0,User 0,admin\nuser1,User 1,member\nuser2,User 2,member\n"
                           "user3,User 3,member\nuser4,User 4,member")
    assert res.lossless is True


def test_typed_values_render_as_json_literals_and_are_lossy():
    text = "[\n" + ",\n".join(
        f'  {{"id": {i}, "price": 1.50, "active": {"true" if i % 2 else "false"}, "note": null}}' for i in range(4)
    ) + "\n]"
    res = run(text)
    assert res.content == ("id,price,active,note\n0,1.50,false,null\n1,1.50,true,null\n"
                           "2,1.50,false,null\n3,1.50,true,null")
    assert res.lossless is False  # 1 vs "1", true vs "true" no longer distinguishable


@pytest.mark.parametrize("text", [
    "[]",                                                              # empty list
    "[\n  {}, {}\n]",                                                  # no columns
    json.dumps([{"a": "x", "b": {"c": 1}}, {"a": "y", "b": {"c": 2}}], indent=2),  # nested object
    json.dumps([{"a": "x", "b": [1]}, {"a": "y", "b": [2]}], indent=2),  # nested array
    json.dumps([{"a": "x", "b": "1"}], indent=2),                     # single record
    json.dumps([{"a": "x", "b": "1"}, {"a": "y", "c": "2"}], indent=2),  # key sets differ
    json.dumps([{"a": "x"}, "y", "z"], indent=2),                     # not all objects
    json.dumps({"rows": [{"a": "x"}, {"a": "y"}]}, indent=2),         # top level is not a list
    '[\n  {"a": "x", "a": "y"},\n  {"a": "z", "a": "w"}\n]',             # duplicate keys
])
def test_not_applicable_unless_top_level_list_of_flat_uniform_records(text):
    assert CsvTable().applicable(tr(text), JSON, Context()) is False


TRICKY = ["a,b", 'say "hi"', "line1\nline2", "cr\ronly", "crlf\r\nend", "  padded  ", "", '"', "back\\slash",
          "café \U0001F600"]


def test_special_characters_are_quoted_and_parse_back_exactly():
    obj = [{"k": f"r{i}", "v": v} for i, v in enumerate(TRICKY)]
    res = run(obj)
    assert parse_csv(res.content) == [["k", "v"]] + [[f"r{i}", v] for i, v in enumerate(TRICKY)]
    assert res.lossless is True
    assert '"a,b"' in res.content and '"say ""hi"""' in res.content


def test_single_column_of_empty_strings_round_trips():
    res = run([{"v": ""}, {"v": ""}, {"v": "x"}])
    assert parse_csv(res.content) == [["v"], [""], [""], ["x"]]


def test_columns_follow_first_record_key_order():
    text = '[\n  {"a": "1", "b": "2"},\n  {"b": "4", "a": "3"}\n]'
    assert parse_csv(run(text).content) == [["a", "b"], ["1", "2"], ["3", "4"]]


def test_not_lossless_when_a_record_orders_its_keys_differently():
    text = '[\n  {"a": "1", "b": "2"},\n  {"b": "4", "a": "3"}\n]'
    assert run(text).lossless is False


def test_already_delimited_tabular_output_is_declined():
    text = "id,name,role\n1,ann,admin\n2,bob,member\n3,cy,member"
    assert run(text, label=Label("tool_result:tabular")) is None


@pytest.mark.parametrize("v", ['"', '""""', "a\nb", "x", ""])
def test_output_is_shorter_than_minified_json_even_for_quote_heavy_values(v):
    obj = [{"k": v}, {"k": v}]
    assert len(run(obj).content) < len(json.dumps(obj, separators=(",", ":"), ensure_ascii=False))


def test_deterministic():
    a, b = run(USERS), run(USERS)
    assert a.content == b.content and a.lossless == b.lossless


def test_non_tool_result_blocks_are_declined():
    user = Block(("messages", 0, "content", 0), "user", "text", json.dumps(USERS, indent=2), msg_index=0)
    assert CsvTable().run(user, Label("user_prompt"), Context()) is None


def test_registered_as_csv_table_and_not_lossless_by_default():
    t = get("csv_table")
    assert isinstance(t, CsvTable) and t.lossless is False  # per-result lossless says when it is
