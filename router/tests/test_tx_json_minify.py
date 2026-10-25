import json

import pytest
from rr_fixtures import claude_code_request

from reprouter.classify import Label, classify_request
from reprouter.request import Block
from reprouter.transforms.base import Context, get
from reprouter.transforms.json_minify import JsonMinify

JSON = Label("tool_result:json")


def tr(text, raw_content=None):
    raw = {"type": "tool_result", "tool_use_id": "tu_1", "content": text if raw_content is None else raw_content}
    return Block(("messages", 2, "content", 0), "user", "tool_result", text, msg_index=2,
                 tool_name="mcp__api__get", tool_use_id="tu_1", raw=raw)


def run(text, label=JSON, **kw):
    return JsonMinify().run(tr(text, **kw), label, Context())


def test_minifies_pretty_printed_object():
    pretty = json.dumps({"name": "reprouter", "tags": ["a", "b"], "ok": True, "n": None}, indent=2)
    res = run(pretty)
    assert res.content == '{"name":"reprouter","tags":["a","b"],"ok":true,"n":null}'
    assert res.lossless is True


def test_number_literals_are_preserved_byte_for_byte():
    text = '{\n  "a": 1.0,\n  "b": 1e5,\n  "c": -0.0,\n  "d": 12345678901234567890.5,\n  "e": 1E-7,\n  "f": 10\n}'
    res = run(text)
    assert res.content == '{"a":1.0,"b":1e5,"c":-0.0,"d":12345678901234567890.5,"e":1E-7,"f":10}'


def test_key_order_and_duplicate_keys_are_preserved():
    res = run('{ "z": 1, "a": 2, "m": {"y": 1, "b": 2}, "a": 3 }')
    assert res.content == '{"z":1,"a":2,"m":{"y":1,"b":2},"a":3}'


def test_declines_when_not_strictly_shorter():
    already = '{"a":[1,2,{"b":null}]}'
    assert JsonMinify().apply(tr(already), JSON, Context()) is None


@pytest.mark.parametrize("text", ["not json at all", "{broken: json}", "  42  ", '  "a string"  ', "", "  null "])
def test_not_applicable_unless_text_is_json_object_or_array(text):
    assert JsonMinify().applicable(tr(text), JSON, Context()) is False


def test_non_tool_result_blocks_are_declined():
    text = json.dumps({"a": [1, 2, 3]}, indent=2)
    user = Block(("messages", 0, "content", 0), "user", "text", text, msg_index=0)
    assert JsonMinify().run(user, Label("user_prompt"), Context()) is None


@pytest.mark.parametrize("kind", ["tool_result:file_read", "tool_result:edit_confirmation",
                                  "tool_result:search_results", "tool_result:error"])
def test_tool_results_not_labelled_json_are_declined(kind):
    # e.g. a Read of a .json file: Edit later needs those exact bytes
    assert run(json.dumps({"a": [1, 2, 3]}, indent=2), label=Label(kind)) is None


def test_list_content_with_only_text_parts_is_minified():
    text = json.dumps({"a": [1, 2, 3]}, indent=2)
    assert run(text, raw_content=[{"type": "text", "text": text}]).content == '{"a":[1,2,3]}'


def test_declines_when_content_has_non_text_parts():
    # replacing content with a string would silently drop the image
    text = json.dumps({"a": [1, 2, 3]}, indent=2)
    parts = [{"type": "text", "text": text}, {"type": "image", "source": {"type": "base64", "data": "AAAA"}}]
    assert run(text, raw_content=parts) is None


def test_strings_keep_content_and_unicode_is_unescaped():
    res = run('{ "s": "caf\\u00e9 \\"q\\" \\\\ \\n", "e": "\\ud83d\\ude00" }')
    assert res.content == '{"s":"caf\u00e9 \\"q\\" \\\\ \\n","e":"\U0001F600"}'
    assert json.loads(res.content) == {"s": 'caf\u00e9 "q" \\ \n', "e": "\U0001F600"}


def test_lone_surrogate_escape_declines_rather_than_emit_unencodable_text():
    res = run('{ "s": "\\ud800 x" }')
    assert res is None


PAYLOAD = json.dumps({"items": [{"id": i, "sha": f"{i:040x}", "price": i + 0.5, "tags": ["x", "y"]}
                                for i in range(5)], "next": None}, indent=4)


def test_output_parses_to_the_same_value():
    assert json.loads(run(PAYLOAD).content) == json.loads(PAYLOAD)


def test_deterministic():
    a, b = run(PAYLOAD), run(PAYLOAD)
    assert a.content == b.content and a.lossless == b.lossless


def test_applies_to_mcp_json_result_as_labelled_by_the_classifier():
    req = claude_code_request()
    req["messages"] += [
        {"role": "assistant", "content": [{"type": "tool_use", "id": "tu_3", "name": "mcp__api__get", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tu_3", "content": PAYLOAD}]},
    ]
    results = {b.path: JsonMinify().run(b, label, Context()) for b, label in classify_request(req)}
    changed = {p for p, r in results.items() if r is not None}
    assert changed == {("messages", 6, "content", 0)}  # only the JSON result; Read/Bash output untouched


def test_registered_as_lossless_json_minify():
    t = get("json_minify")
    assert isinstance(t, JsonMinify) and t.lossless is True
