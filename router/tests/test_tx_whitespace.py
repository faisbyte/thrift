import pytest

import reprouter.transforms.whitespace  # noqa: F401  (registers the transform)
from reprouter.classify import Label
from reprouter.request import Block
from reprouter.transforms.base import Context, get


def tx():
    return get("whitespace")


def result_block(text, tool="Bash"):
    return Block(("messages", 2, "content", 0), "user", "tool_result", text, msg_index=2, tool_name=tool,
                 raw={"type": "tool_result", "tool_use_id": "tu_1", "content": text})


def run(text, kind="tool_result:command_output", tool="Bash"):
    return tx().run(result_block(text, tool), Label(kind), Context())


def test_strips_ansi_color_codes():
    text = "\x1b[32mPASSED\x1b[0m tests/test_a.py::test_one\n\x1b[1;31mFAILED\x1b[0m tests/test_a.py::test_two\n"
    res = run(text)
    assert res.content == "PASSED tests/test_a.py::test_one\nFAILED tests/test_a.py::test_two\n"


def test_strips_osc_sequences_hyperlinks_and_titles():
    text = ("\x1b]0;npm install\x07added 12 packages\n"
            "see \x1b]8;;https://docs.npmjs.com\x1b\\the docs\x1b]8;;\x1b\\ for details\n")
    res = run(text)
    assert res.content == "added 12 packages\nsee the docs for details\n"


def test_converts_crlf_to_lf():
    res = run("Build started\r\nCompiling main.c\r\nBuild succeeded\r\n")
    assert res.content == "Build started\nCompiling main.c\nBuild succeeded\n"


def test_keeps_only_last_carriage_return_overwrite_on_a_line():
    text = ("Collecting torch\n"
            "Downloading torch.whl   0%|          |\rDownloading torch.whl  50%|#####     |"
            "\rDownloading torch.whl 100%|##########|\n"
            "Successfully installed torch\n")
    res = run(text)
    assert res.content == ("Collecting torch\nDownloading torch.whl 100%|##########|\n"
                           "Successfully installed torch\n")


def test_trailing_carriage_return_keeps_last_visible_text():
    res = run("step 1/3\rstep 2/3\rstep 3/3\r\r\ndone\n")
    assert res.content == "step 3/3\ndone\n"


def test_strips_trailing_spaces_and_tabs_but_not_leading_indent():
    text = "NAME        READY   STATUS    \t\n  web-7d9f   1/1     Running   \n"
    res = run(text)
    assert res.content == "NAME        READY   STATUS\n  web-7d9f   1/1     Running\n"


def test_collapses_three_or_more_blank_lines_to_one():
    text = "section one\n\n\n\n\nsection two\n\n\nsection three\n\nsection four\n"
    res = run(text)
    assert res.content == "section one\n\nsection two\n\n\nsection three\n\nsection four\n"


# search_results deliberately keep trailing spaces (see test_search_results_keep_trailing_spaces...)
@pytest.mark.parametrize("kind", ["tool_result:command_output", "tool_result:log", "tool_result:test_output",
                                  "tool_result:error"])
def test_applies_to_command_style_results(kind):
    assert run("ok   \n", kind=kind).content == "ok\n"


@pytest.mark.parametrize("kind", ["tool_result:file_read", "tool_result:diff", "tool_result:json",
                                  "tool_result:edit_confirmation", "tool_result:tabular"])
def test_declines_file_reads_and_other_result_kinds(kind):
    assert run("1\tx = 1   \n2\t\x1b[0m\n", kind=kind) is None


def test_declines_non_tool_result_blocks():
    text_blk = Block(("messages", 0, "content", 0), "user", "text", "hello   \n\x1b[31mred\x1b[0m", msg_index=0)
    use_blk = Block(("messages", 1, "content", 0), "assistant", "tool_use", "cmd   \n", msg_index=1,
                    tool_name="Bash", tool_input={"command": "ls   "})
    assert tx().run(text_blk, Label("user_prompt"), Context()) is None
    assert tx().run(use_blk, Label("tool_use"), Context()) is None


def test_apply_declines_when_already_clean():
    clean = "total 8\n-rw-r--r--  1 dev  staff  120 Oct 25 10:00 README.md\n"
    assert tx().apply(result_block(clean), Label("tool_result:command_output"), Context()) is None


def test_declines_results_with_non_text_parts():
    raw = {"type": "tool_result", "tool_use_id": "tu_9", "content": [
        {"type": "text", "text": "screenshot   \n"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "iVBORw0K"}},
    ]}
    blk = Block(("messages", 2, "content", 0), "user", "tool_result", "screenshot   \n", msg_index=2,
                tool_name="Bash", raw=raw)
    assert tx().run(blk, Label("tool_result:command_output"), Context()) is None


def test_result_is_marked_lossless():
    assert tx().lossless is True
    assert run("\x1b[1mbold\x1b[0m\n").lossless is True


def test_deterministic_byte_identical_output():
    text = "\x1b[33mwarn\x1b[0m   \r\n50%\r100%\n\n\n\n\nend\t\n"
    outs = {run(text).content for _ in range(5)}
    assert outs == {"warn\n100%\n\nend\n"}


def test_whitespace_only_lines_count_as_blank_when_collapsing():
    res = run("a\n   \n\t\n  \nb\n")
    assert res.content == "a\n\nb\n"


def test_search_results_keep_trailing_spaces_because_they_are_code():
    from reprouter.classify import Label
    from reprouter.request import Block
    from reprouter.transforms.base import Context
    from reprouter.transforms.whitespace import Whitespace as WhitespaceTransform

    text = "src/a.py:3:    x = 1   \nsrc/a.py:9:\x1b[31mdef f():\x1b[0m  "
    b = Block(("messages", 1, "content", 0), "user", "tool_result", text, msg_index=1, tool_name="Grep")
    res = WhitespaceTransform().run(b, Label("tool_result:search_results"), Context())
    assert res is not None
    assert res.content == "src/a.py:3:    x = 1   \nsrc/a.py:9:def f():  "


def test_whitespace_runs_in_the_cleanup_stage():
    from reprouter.transforms.whitespace import Whitespace as WhitespaceTransform

    assert WhitespaceTransform.stage == "cleanup"
