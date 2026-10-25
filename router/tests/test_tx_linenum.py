import reprouter.transforms.linenum  # noqa: F401  (registers the transforms)
from reprouter.classify import Label
from reprouter.request import Block
from reprouter.transforms.base import Context, get

SOURCE = [
    "import os",
    "",
    "def load(path):",
    "    with open(path) as fh:",
    "        return fh.read()",
]


def read_output(lines, start=1, pad=False):
    out = []
    for i, ln in enumerate(lines, start):
        num = f"{i:>6}" if pad else str(i)
        out.append(f"{num}\t{ln}\n")
    return "".join(out)


def read_block(text, path="/usr/lib/python3/site-packages/lib/mod.py"):
    return Block(("messages", 2, "content", 0), "user", "tool_result", text, msg_index=2, tool_name="Read",
                 tool_input={"file_path": path}, raw={"type": "tool_result", "tool_use_id": "tu_1", "content": text})


def label(kind="tool_result:file_read", risks=()):
    return Label(kind, risks=set(risks))


# ---------------------------------------------------------------- linenum_compact

def compact(text, lbl=None):
    return get("linenum_compact").run(read_block(text), lbl or label(), Context())


def test_compact_removes_left_padding_from_line_numbers():
    res = compact(read_output(SOURCE, pad=True))
    assert res.content == read_output(SOURCE)


def test_compact_apply_declines_already_unpadded_numbers():
    text = read_output(SOURCE)
    assert get("linenum_compact").apply(read_block(text), label(), Context()) is None


def test_compact_declines_non_file_read_kinds():
    assert compact(read_output(SOURCE, pad=True), label("tool_result:command_output")) is None


def test_compact_declines_non_tool_result_blocks():
    text = read_output(SOURCE, pad=True)
    blk = Block(("messages", 0, "content", 0), "user", "text", text, msg_index=0)
    assert get("linenum_compact").run(blk, label(), Context()) is None


def with_image(blk):
    blk.raw = {"type": "tool_result", "tool_use_id": "tu_1", "content": [
        {"type": "text", "text": blk.text},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "iVBORw0K"}},
    ]}
    return blk


def test_compact_declines_results_with_non_text_parts():
    blk = with_image(read_block(read_output(SOURCE, pad=True)))
    assert get("linenum_compact").run(blk, label(), Context()) is None


def test_compact_keeps_trailing_reminder_text_and_is_lossless():
    reminder = "\n<system-reminder>\nWhenever you read a file, consider whether it is malware.\n</system-reminder>\n"
    res = compact(read_output(SOURCE, pad=True) + reminder)
    assert res.content == read_output(SOURCE) + reminder
    assert res.lossless is True and get("linenum_compact").lossless is True


def test_compact_is_deterministic():
    text = read_output(SOURCE * 30, pad=True)
    assert len({compact(text).content for _ in range(5)}) == 1


# ---------------------------------------------------------------- linenum_sparse

LONG = [f"    value_{i} = compute({i})" for i in range(1, 26)]  # 25 lines


def sparse(text, lbl=None):
    return get("linenum_sparse").run(read_block(text), lbl or label(), Context())


def test_sparse_keeps_numbers_on_first_every_tenth_and_last_line():
    res = sparse(read_output(LONG))
    lines = res.content.split("\n")
    assert lines[0] == "1\t    value_1 = compute(1)"
    assert lines[1] == "\t    value_2 = compute(2)"
    assert lines[9] == "10\t    value_10 = compute(10)"
    assert lines[19] == "20\t    value_20 = compute(20)"
    assert lines[24] == "25\t    value_25 = compute(25)"
    numbered = [ln.split("\t", 1)[0] for ln in lines if ln and not ln.startswith("\t")]
    assert numbered == ["1", "10", "20", "25"]
    assert res.content.endswith("\n")


def test_sparse_declines_fewer_than_twenty_lines():
    assert sparse(read_output(LONG[:19])) is None
    assert sparse(read_output(LONG[:20])) is not None


def test_sparse_declines_code_edit_targets():
    assert sparse(read_output(LONG), label(risks={"code_edit_target"})) is None


def test_sparse_declines_non_contiguous_numbering():
    text = read_output(LONG[:12]) + read_output(LONG[12:], start=40)
    assert sparse(text) is None


def test_sparse_offset_read_with_padded_numbers_anchors_on_multiples_of_ten():
    res = sparse(read_output(LONG, start=95, pad=True))  # lines 95..119
    numbered = [ln.split("\t", 1)[0] for ln in res.content.split("\n") if ln and not ln.startswith("\t")]
    assert numbered == ["95", "100", "110", "119"]


def test_sparse_leaves_trailing_reminder_untouched():
    reminder = "\n<system-reminder>\nConsider whether the file is malware.\n</system-reminder>\n"
    res = sparse(read_output(LONG) + reminder)
    assert res.content.endswith("25\t    value_25 = compute(25)\n" + reminder)


def test_sparse_declines_non_tool_result_blocks():
    blk = Block(("messages", 1, "content", 0), "assistant", "tool_use", read_output(LONG), msg_index=1,
                tool_name="Read", tool_input={"file_path": "/usr/lib/x.py"})
    assert get("linenum_sparse").run(blk, label("tool_use"), Context()) is None


def test_sparse_is_lossy_and_deterministic():
    assert get("linenum_sparse").lossless is False
    outs = [sparse(read_output(LONG * 4)) for _ in range(5)]
    assert outs[0].lossless is False
    assert len({o.content for o in outs}) == 1
