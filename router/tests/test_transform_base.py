import pytest

from reprouter.classify import Label
from reprouter.request import Block
from reprouter.transforms.base import Context, Result, Transform, get, register, registry


class Upper(Transform):
    name = "test_upper"
    lossless = False

    def applicable(self, block, label, ctx):
        return block.type == "tool_result"

    def apply(self, block, label, ctx):
        return Result(content=block.text.upper(), lossless=False)


def blk(text="abc"):
    return Block(("messages", 1, "content", 0), "user", "tool_result", text, msg_index=1, tool_name="Bash")


def test_register_and_lookup():
    t = register(Upper())
    assert get("test_upper") is t
    assert "test_upper" in registry()


def test_duplicate_registration_rejected():
    register(Upper()) if "test_upper" not in registry() else None
    with pytest.raises(ValueError):
        register(Upper())


def test_result_text_property_for_string_and_blocks():
    assert Result(content="hi", lossless=True).text == "hi"
    r = Result(content=[{"type": "image", "source": {}}, {"type": "text", "text": "facts"}], lossless=False)
    assert r.text == "facts" and not r.text_only


def test_run_declines_when_not_applicable_or_unchanged():
    t = get("test_upper")
    ctx = Context()
    nonresult = Block(("system", 0), "system", "text", "abc")
    assert t.run(nonresult, Label("system_prompt"), ctx) is None
    assert t.run(blk("ABC"), Label("tool_result:command_output"), ctx) is None  # no change
    assert t.run(blk("abc"), Label("tool_result:command_output"), ctx).content == "ABC"


def test_run_fails_closed_on_transform_exception():
    class Boom(Transform):
        name = "test_boom"
        lossless = True

        def applicable(self, block, label, ctx):
            return True

        def apply(self, block, label, ctx):
            raise RuntimeError("bug")

    assert Boom().run(blk(), Label("x"), Context()) is None
