import json

from reprouter.classify import Label
from reprouter.request import Block
from reprouter.transforms.base import Context
from reprouter.transforms.tool_input_elide import ToolInputElide

BIG = "\n".join(f"line {i}: value = compute({i}) + offset_{i}" for i in range(120))


def tu(name, inp):
    return Block(("messages", 3, "content", 1), "assistant", "tool_use", json.dumps(inp), msg_index=3,
                 tool_name=name, tool_input=inp, tool_use_id="tu_9")


def run(block):
    return ToolInputElide().run(block, Label("tool_use"), Context())


def test_large_write_content_is_elided_keeping_head_tail_and_other_keys():
    res = run(tu("Write", {"file_path": "/w/big.py", "content": BIG}))
    assert isinstance(res.content, dict) and not res.lossless
    assert list(res.content) == ["file_path", "content"] and res.content["file_path"] == "/w/big.py"
    c = res.content["content"]
    assert c.startswith("line 0: value") and c.rstrip().endswith("offset_119")
    assert "line 60:" not in c
    assert "reprouter" in c and "Read it" in c and "115 more lines" in c


def test_edit_strings_elided_independently():
    res = run(tu("Edit", {"file_path": "/w/a.py", "old_string": "x = 1", "new_string": BIG, "replace_all": False}))
    assert res.content["old_string"] == "x = 1" and res.content["replace_all"] is False
    assert "elided" in res.content["new_string"]


def test_multiedit_edits_list():
    res = run(tu("MultiEdit", {"file_path": "/w/a.py", "edits": [{"old_string": BIG, "new_string": "y"},
                                                                  {"old_string": "a", "new_string": "b"}]}))
    assert "elided" in res.content["edits"][0]["old_string"] and res.content["edits"][1] == {"old_string": "a",
                                                                                              "new_string": "b"}


def test_bash_heredoc_script_elided_with_output_hint():
    cmd = "python3 - <<'EOF'\n" + BIG + "\nEOF"
    res = run(tu("Bash", {"command": cmd, "description": "run analysis"}))
    assert res.content["command"].startswith("python3 - <<'EOF'")
    assert "already ran" in res.content["command"] and res.content["description"] == "run analysis"


def test_small_inputs_and_other_tools_untouched():
    assert run(tu("Write", {"file_path": "/w/s.py", "content": "print(1)\n"})) is None
    assert run(tu("Read", {"file_path": "/w/" + "x" * 2000})) is None
    b = Block(("messages", 2, "content", 0), "user", "tool_result", BIG, msg_index=2, tool_name="Bash")
    assert ToolInputElide().run(b, Label("tool_result:command_output"), Context()) is None


def test_deterministic():
    b = tu("Write", {"file_path": "/w/big.py", "content": BIG})
    assert run(b).content == run(b).content
