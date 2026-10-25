from rr_fixtures import claude_code_request

from reprouter.classify import classify_request
from reprouter.transforms.base import Context, get

import reprouter.transforms.dedup  # noqa: F401  (registers "dedup")

T = get("dedup")
BIG = "".join(f"line {i}: collected output that is long enough to matter\n" for i in range(20))


def convo(*calls):
    """Build a Claude Code-shaped request from (tool_name, input, output[, is_error]) tuples.

    Each call gets tool_use_id tu_1, tu_2, ... in order.
    """
    msgs = [{"role": "user", "content": [{"type": "text", "text": "please do the task"}]}]
    for i, call in enumerate(calls, 1):
        name, inp, out = call[:3]
        result = {"type": "tool_result", "tool_use_id": f"tu_{i}", "content": out}
        if len(call) > 3 and call[3]:
            result["is_error"] = True
        msgs.append({"role": "assistant", "content": [{"type": "tool_use", "id": f"tu_{i}", "name": name, "input": inp}]})
        msgs.append({"role": "user", "content": [result]})
    req = claude_code_request()
    req["messages"] = msgs
    return req


def run_at(req, tool_use_id):
    """applicable + apply without run()'s exception swallowing, so a crash can't pass as a decline."""
    pairs = classify_request(req)
    for i, (b, lab) in enumerate(pairs):
        if b.type == "tool_result" and b.tool_use_id == tool_use_id:
            ctx = Context(project_root="/work/proj", prior=pairs[:i])
            return T.apply(b, lab, ctx) if T.applicable(b, lab, ctx) else None
    raise AssertionError(f"no tool_result {tool_use_id}")


def test_identical_bash_output_replaced_by_reference_to_earlier_result():
    req = convo(("Bash", {"command": "pytest -q"}, BIG), ("Bash", {"command": "pytest -q"}, BIG))
    res = run_at(req, "tu_2")
    assert res.content == (
        "[reprouter: identical to the earlier Bash result (tool_use_id tu_1, pytest -q); output unchanged]"
    )
    assert res.lossless is True
    assert T.name == "dedup" and T.lossless is True


def test_read_reference_summarises_file_path():
    req = convo(("Read", {"file_path": "/work/proj/m.py"}, BIG), ("Read", {"file_path": "/work/proj/m.py"}, BIG))
    assert run_at(req, "tu_2").content == (
        "[reprouter: identical to the earlier Read result (tool_use_id tu_1, /work/proj/m.py); output unchanged]"
    )


def test_grep_reference_summarises_pattern():
    req = convo(("Grep", {"pattern": "def sub", "path": "/work/proj"}, BIG),
                ("Grep", {"pattern": "def sub", "path": "/work/proj"}, BIG))
    assert run_at(req, "tu_2").content == (
        "[reprouter: identical to the earlier Grep result (tool_use_id tu_1, def sub); output unchanged]"
    )


def test_long_bash_command_truncated_to_80_chars():
    cmd = "cd /work/proj && python -m pytest -q tests/test_everything.py -k 'not slow' --maxfail=1 -x"
    assert len(cmd) > 80
    req = convo(("Bash", {"command": cmd}, BIG), ("Bash", {"command": "make test"}, BIG))
    content = run_at(req, "tu_2").content
    summary = content.split("tu_1, ", 1)[1].rsplit("); output unchanged]", 1)[0]
    assert len(summary) == 80
    assert summary == cmd[:79] + "…"


def test_multiline_bash_command_collapsed_to_one_line():
    cmd = "cat <<'EOF' > x.txt\nhello\nEOF\n  wc -l x.txt"
    req = convo(("Bash", {"command": cmd}, BIG), ("Bash", {"command": cmd}, BIG))
    assert "(tool_use_id tu_1, cat <<'EOF' > x.txt hello EOF wc -l x.txt);" in run_at(req, "tu_2").content


def test_references_first_earlier_occurrence():
    req = convo(("Bash", {"command": "a"}, BIG), ("Bash", {"command": "b"}, BIG), ("Bash", {"command": "c"}, BIG))
    assert "(tool_use_id tu_1, a)" in run_at(req, "tu_3").content


def test_no_earlier_identical_result_declined():
    req = convo(("Bash", {"command": "a"}, BIG), ("Bash", {"command": "a"}, BIG + "changed\n"))
    assert run_at(req, "tu_1") is None
    assert run_at(req, "tu_2") is None


def test_identical_text_in_non_tool_result_does_not_count_as_earlier_result():
    req = convo(("Bash", {"command": "a"}, BIG))
    req["messages"][0]["content"][0]["text"] = BIG  # user pasted the same text earlier
    assert run_at(req, "tu_1") is None


def test_non_tool_result_blocks_declined():
    req = convo(("Bash", {"command": "a"}, BIG))
    req["messages"].append({"role": "user", "content": [{"type": "text", "text": BIG}]})
    req["messages"].append({"role": "assistant", "content": [{"type": "text", "text": BIG}]})
    pairs = classify_request(req)
    for i, (b, lab) in enumerate(pairs):
        if b.type != "tool_result":
            assert T.run(b, lab, Context(prior=pairs[:i])) is None


def test_deterministic_for_identical_inputs():
    a = run_at(convo(("Bash", {"command": "pytest"}, BIG), ("Bash", {"command": "pytest"}, BIG)), "tu_2")
    b = run_at(convo(("Bash", {"command": "pytest"}, BIG), ("Bash", {"command": "pytest"}, BIG)), "tu_2")
    assert a == b and a is not None


def test_short_identical_output_declined():
    short = "x" * 199
    req = convo(("Bash", {"command": "ls"}, short), ("Bash", {"command": "ls"}, short))
    assert run_at(req, "tu_2") is None
    req = convo(("Bash", {"command": "ls"}, short + "x"), ("Bash", {"command": "ls"}, short + "x"))
    assert run_at(req, "tu_2") is not None  # exactly 200 chars qualifies


def test_error_result_declined():
    req = convo(("Bash", {"command": "make"}, BIG), ("Bash", {"command": "make"}, BIG, True))
    assert run_at(req, "tu_2") is None


def test_result_with_image_content_declined():
    content = [{"type": "text", "text": BIG}, {"type": "image", "source": {"type": "base64", "data": "AAAA"}}]
    req = convo(("Read", {"file_path": "/work/proj/a.png"}, content), ("Read", {"file_path": "/work/proj/a.png"}, content))
    assert run_at(req, "tu_2") is None


def test_declined_when_reference_would_not_be_shorter():
    long_path = "/work/proj/" + "deeply/nested/" * 20 + "f.txt"
    text = "y" * 250
    req = convo(("Read", {"file_path": long_path}, text), ("Read", {"file_path": long_path}, text))
    assert run_at(req, "tu_2") is None


def test_unknown_tool_reference_has_no_input_summary():
    req = convo(("WebFetch", {"url": "https://x.test"}, BIG), ("WebFetch", {"url": "https://x.test"}, BIG))
    assert run_at(req, "tu_2").content == (
        "[reprouter: identical to the earlier WebFetch result (tool_use_id tu_1); output unchanged]"
    )
