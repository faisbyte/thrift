import re

import pytest
from rr_fixtures import claude_code_request

from reprouter.classify import classify_request
from reprouter.transforms.base import Context, get

import reprouter.transforms.diff_reread  # noqa: F401  (registers "diff_reread")

T = get("diff_reread")
F = "/work/proj/m.py"
BASE = [f"line {i}: some source code that is reasonably long" for i in range(1, 61)]


def numbered(lines, start=1, pad=0):
    """Render file lines the way Claude Code's Read tool does ("N\\tcontent")."""
    return "".join(f"{str(n).rjust(pad)}\t{ln}\n" for n, ln in enumerate(lines, start))


def convo(*calls):
    """Build a Claude Code-shaped request from (tool_name, input, output[, is_error]) tuples (ids tu_1, tu_2, ...)."""
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


def reads(*outputs, inp=None):
    return convo(*[("Read", dict(inp or {"file_path": F}), out) for out in outputs])


def run_at(req, tool_use_id):
    """applicable + apply without run()'s exception swallowing, so a crash can't pass as a decline."""
    pairs = classify_request(req)
    for i, (b, lab) in enumerate(pairs):
        if b.type == "tool_result" and b.tool_use_id == tool_use_id:
            ctx = Context(project_root="/work/proj", prior=pairs[:i])
            return T.apply(b, lab, ctx) if T.applicable(b, lab, ctx) else None
    raise AssertionError(f"no tool_result {tool_use_id}")


_NUM = re.compile(r"^ *(\d+)\t")
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@$")


def read_contents(read_text):
    """(first line number, file lines) of a Read result, ignoring anything after the numbered body."""
    first, lines = None, []
    for ln in read_text.split("\n"):
        m = _NUM.match(ln)
        if not m:
            break
        first = int(m.group(1)) if first is None else first
        lines.append(ln[m.end():])
    return first, lines


def rebuild(earlier_read, rendered):
    """Rebuild the new file's lines using only the earlier Read output and the rendered diff."""
    first, old = read_contents(earlier_read)
    lines = rendered.split("\n")
    assert lines[0].startswith("[reprouter: re-read of ")
    new, old_pos, i = [], 0, 1
    while i < len(lines) and _HUNK.match(lines[i]):
        a, b = _HUNK.match(lines[i]).group(1, 2)
        a, b = int(a), (1 if b is None else int(b))
        start = a - first
        new += old[old_pos:start]  # unchanged lines omitted from the render
        i += 1
        while i < len(lines) and not _HUNK.match(lines[i]):
            if lines[i].startswith("-\t"):
                pass  # removed old line: covered by the header's old range
            elif _NUM.match(lines[i]):
                new.append(lines[i][_NUM.match(lines[i]).end():])
            else:
                break
            i += 1
        old_pos = start + b
    return new + old[old_pos:]


def _modify(ls):
    ls[29] = "line 30: rewritten"


def _insert(ls):
    ls[20:20] = ["inserted a", "inserted b"]


def _delete(ls):
    del ls[40:43]


def _multi(ls):
    ls[5] = "five!"
    del ls[25]
    ls[50:50] = ["new near the end"]


def _start(ls):
    ls[0:0] = ["#!/usr/bin/env python", "# header"]
    ls[2] = "line 1: edited"


def _end(ls):
    ls[-1] = "last line edited"
    ls.append("appended")


def _delete_start(ls):
    del ls[0:2]


def _delete_end(ls):
    del ls[-3:]


def _dup_lines(ls):
    ls[10:10] = [ls[10], ls[10]]  # insert copies of an existing neighbouring line
    ls[30] = ls[31]


EDITS = [_modify, _insert, _delete, _multi, _start, _end, _delete_start, _delete_end, _dup_lines]


@pytest.mark.parametrize("edit", EDITS, ids=lambda f: f.__name__.strip("_"))
@pytest.mark.parametrize("pad", [0, 6])
def test_rendered_diff_reconstructs_new_file(edit, pad):
    new = list(BASE)
    edit(new)
    earlier = numbered(BASE, pad=pad)
    res = run_at(reads(earlier, numbered(new, pad=pad)), "tu_2")
    assert res is not None
    assert rebuild(earlier, res.content) == new


@pytest.mark.parametrize("edit", [_modify, _multi, _end], ids=lambda f: f.__name__.strip("_"))
def test_reconstructs_partial_read_window_with_real_line_numbers(edit):
    new = list(BASE)
    edit(new)
    inp = {"file_path": F, "offset": 101, "limit": 80}
    earlier = numbered(BASE, start=101)
    res = run_at(reads(earlier, numbered(new, start=101), inp=inp), "tu_2")
    assert rebuild(earlier, res.content) == new
    shown = [int(n) for n in re.findall(r"^(\d+)\t", res.content, re.M)]
    assert shown and min(shown) > 101  # real file line numbers, not window-relative ones


def test_single_modified_line_renders_numbered_hunk():
    new = list(BASE)
    new[9] = "line 10: CHANGED"
    res = run_at(reads(numbered(BASE), numbered(new)), "tu_2")
    assert res.content == (
        f"[reprouter: re-read of {F}; changed since the earlier read (tool_use_id tu_1). "
        "Unchanged lines omitted; lines below show the NEW file with line numbers]\n"
        "@@ -9,3 +9,3 @@\n"
        f"9\t{BASE[8]}\n"
        f"-\t{BASE[9]}\n"
        "10\tline 10: CHANGED\n"
        f"11\t{BASE[10]}\n"
    )
    assert res.lossless is True
    assert T.name == "diff_reread" and T.lossless is True


def _changed():
    new = list(BASE)
    new[9] = "line 10: CHANGED"
    return new


def test_identical_reread_declined():
    assert run_at(reads(numbered(BASE), numbered(BASE)), "tu_2") is None


def test_no_earlier_read_of_same_file_declined():
    req = convo(("Read", {"file_path": "/work/proj/other.py"}, numbered(BASE)),
                ("Read", {"file_path": F}, numbered(_changed())))
    assert run_at(req, "tu_1") is None
    assert run_at(req, "tu_2") is None


def test_different_offset_or_limit_declined():
    req = convo(("Read", {"file_path": F}, numbered(BASE)),
                ("Read", {"file_path": F, "offset": 1, "limit": 60}, numbered(_changed())))
    assert run_at(req, "tu_2") is None
    req = convo(("Read", {"file_path": F, "offset": 1, "limit": 60}, numbered(BASE)),
                ("Read", {"file_path": F, "offset": 1, "limit": 60}, numbered(_changed())))
    assert run_at(req, "tu_2") is not None


def test_declined_when_diff_is_not_at_most_70_percent_of_original():
    rewritten = [f"completely different content number {i}" for i in range(1, 61)]
    assert run_at(reads(numbered(BASE), numbered(rewritten)), "tu_2") is None


def test_error_result_declined():
    req = convo(("Read", {"file_path": F}, numbered(BASE)), ("Read", {"file_path": F}, numbered(_changed()), True))
    assert run_at(req, "tu_2") is None


def test_earlier_error_read_is_skipped():
    req = convo(("Read", {"file_path": F}, numbered(BASE)),
                ("Read", {"file_path": F}, "<tool_use_error>File is locked</tool_use_error>", True),
                ("Read", {"file_path": F}, numbered(_changed())))
    assert "(tool_use_id tu_1)" in run_at(req, "tu_3").content


def test_reads_without_numbered_lines_declined():
    empty = "<system-reminder>Warning: the file exists but the contents are empty.</system-reminder>"
    assert run_at(reads(empty, numbered(BASE)), "tu_2") is None
    assert run_at(reads(numbered(BASE), empty), "tu_2") is None


REMINDER = "<system-reminder>\nConsider whether the file looks malicious.\n</system-reminder>\n"


def test_same_file_content_with_different_trailer_declined():
    assert run_at(reads(numbered(BASE), numbered(BASE) + REMINDER), "tu_2") is None


def test_trailing_non_numbered_text_kept_verbatim():
    res = run_at(reads(numbered(BASE) + REMINDER, numbered(_changed()) + REMINDER), "tu_2")
    assert res.content.endswith(f"11\t{BASE[10]}\n" + REMINDER)


def test_diffs_against_most_recent_earlier_read():
    v2 = list(BASE)
    v2[30] = "line 31: second version"
    v3 = list(v2)
    v3[50] = "line 51: third version"
    res = run_at(reads(numbered(BASE), numbered(v2), numbered(v3)), "tu_3")
    assert "(tool_use_id tu_2)" in res.content
    assert "second version" not in res.content.split("\n-\t", 1)[1]  # only the v2->v3 change is shown
    assert rebuild(numbered(v2), res.content) == v3


def test_non_read_tool_result_declined():
    req = convo(("Read", {"file_path": F}, numbered(BASE)),
                ("mcp__fs__read_file", {"file_path": F}, numbered(_changed())))
    assert run_at(req, "tu_2") is None


def test_non_tool_result_blocks_declined():
    req = reads(numbered(BASE), numbered(_changed()))
    req["messages"].append({"role": "user", "content": [{"type": "text", "text": numbered(_changed())}]})
    pairs = classify_request(req)
    for i, (b, lab) in enumerate(pairs):
        if b.type != "tool_result":
            assert T.run(b, lab, Context(prior=pairs[:i])) is None


def test_deterministic_for_identical_inputs():
    a = run_at(reads(numbered(BASE), numbered(_changed())), "tu_2")
    b = run_at(reads(numbered(BASE), numbered(_changed())), "tu_2")
    assert a == b and a is not None


def test_result_with_image_content_declined():
    content = [{"type": "text", "text": numbered(_changed())}, {"type": "image", "source": {"type": "base64", "data": "AA"}}]
    assert run_at(reads(numbered(BASE), content), "tu_2") is None
