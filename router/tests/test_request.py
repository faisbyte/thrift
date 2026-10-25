import copy
import json

from rr_fixtures import READ_OUT, claude_code_request
from reprouter.request import apply_replacements, parse_blocks


def test_parse_yields_system_tool_and_message_blocks_in_prompt_order():
    blocks = parse_blocks(claude_code_request())
    paths = [b.path for b in blocks]
    assert paths[:3] == [("system", 0), ("system", 1), ("system", 2)]
    assert paths[3:5] == [("tools", 0), ("tools", 1)]
    assert ("messages", 2, "content", 0) in paths
    assert [b.path for b in blocks] == sorted(paths, key=lambda p: paths.index(p))  # stable order


def test_tool_result_block_knows_its_tool_name_and_input():
    blocks = {b.path: b for b in parse_blocks(claude_code_request())}
    read = blocks[("messages", 2, "content", 0)]
    assert read.type == "tool_result"
    assert read.tool_name == "Read"
    assert read.tool_input == {"file_path": "/work/proj/m.py"}
    assert read.text == READ_OUT
    bash = blocks[("messages", 4, "content", 0)]
    assert bash.tool_name == "Bash" and bash.text.startswith("....F")


def test_blocks_carry_message_index_role_and_cache_marker():
    blocks = {b.path: b for b in parse_blocks(claude_code_request())}
    assert blocks[("system", 1)].role == "system" and blocks[("system", 1)].has_cache_control
    assert blocks[("tools", 0)].role == "tools"
    assert blocks[("messages", 1, "content", 1)].role == "assistant"
    assert blocks[("messages", 1, "content", 1)].msg_index == 1
    assert blocks[("messages", 4, "content", 0)].has_cache_control
    assert not blocks[("messages", 2, "content", 0)].has_cache_control


def test_string_message_content_is_a_single_text_block():
    req = claude_code_request()
    req["messages"][0]["content"] = "plain prompt"
    blocks = {b.path: b for b in parse_blocks(req)}
    b = blocks[("messages", 0, "content")]
    assert b.type == "text" and b.text == "plain prompt"


def test_apply_replaces_tool_result_text_and_keeps_cache_control():
    req = claude_code_request()
    out = apply_replacements(req, {("messages", 4, "content", 0): "1 failed: test_m.py::test_sub"})
    tr = out["messages"][4]["content"][0]
    assert tr["content"] == "1 failed: test_m.py::test_sub"
    assert tr["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert tr["tool_use_id"] == "tu_2"


def test_apply_does_not_mutate_input_and_leaves_other_bytes_identical():
    req = claude_code_request()
    before = copy.deepcopy(req)
    out = apply_replacements(req, {("messages", 2, "content", 0): "short"})
    assert req == before
    out["messages"][2]["content"][0]["content"] = READ_OUT
    assert json.dumps(out, sort_keys=True) == json.dumps(before, sort_keys=True)


def test_apply_can_replace_tool_result_with_content_blocks():
    req = claude_code_request()
    img = [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAA="}},
           {"type": "text", "text": "facts: m.py"}]
    out = apply_replacements(req, {("messages", 2, "content", 0): img})
    assert out["messages"][2]["content"][0]["content"] == img


def test_apply_replaces_plain_text_block():
    req = claude_code_request()
    out = apply_replacements(req, {("system", 2): "# Env\ncwd: /work/proj"})
    assert out["system"][2] == {"type": "text", "text": "# Env\ncwd: /work/proj", "cache_control": {"type": "ephemeral"}}


def test_apply_with_no_replacements_returns_equal_copy():
    req = claude_code_request()
    assert apply_replacements(req, {}) == req


def test_tool_use_block_text_is_its_input_json():
    blocks = {b.path: b for b in parse_blocks(claude_code_request())}
    tu = blocks[("messages", 1, "content", 1)]
    assert json.loads(tu.text) == {"file_path": "/work/proj/m.py"}


def test_apply_replaces_tool_use_input_with_a_dict():
    req = claude_code_request()
    out = apply_replacements(req, {("messages", 1, "content", 1): {"file_path": "/work/proj/OTHER.py"}})
    tu = out["messages"][1]["content"][1]
    assert tu["input"] == {"file_path": "/work/proj/OTHER.py"} and tu["id"] == "tu_1" and tu["name"] == "Read"
