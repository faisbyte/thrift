import base64

from reprouter.classify import Label
from reprouter.request import Block
from reprouter.tokens import image_tokens
from reprouter.transforms.base import Context
from reprouter.transforms.image import ImageTransform, extract_factsheet, png_size, render_pages

LOG = "\n".join(f"worker {i % 7} processed batch {i} in {i * 13 % 997} ms status=ok" for i in range(600))


def blk(text, tool="Bash", tool_input=None):
    return Block(("messages", 3, "content", 0), "user", "tool_result", text, msg_index=3,
                 tool_name=tool, tool_input=tool_input or {"command": "./run.sh"})


def test_render_pages_size_and_pagination():
    pages = render_pages(LOG, cell="6x12", cols=160, rows=60)
    assert len(pages) == 10  # 600 lines / 60 rows
    w, h = pages[0].size
    assert w == 160 * 6 + 2 * 4 and h == 60 * 12 + 2 * 4


def test_long_lines_wrap_instead_of_being_cut():
    pages = render_pages("x" * 500, cell="6x12", cols=160, rows=60)
    assert len(pages) == 1 and pages[0].size[1] == 4 * 12 + 2 * 4  # 4 wrapped rows, page shrinks to content


def test_rendering_is_deterministic_bytes():
    t = ImageTransform()
    a = t.run(blk(LOG), Label("tool_result:log"), Context())
    b = t.run(blk(LOG), Label("tool_result:log"), Context())
    assert a.content == b.content


def test_factsheet_lists_exact_strings_verbatim_and_dedups():
    text = ("commit 9d8f9a372cc5916fd4781fec207ced7ddac2f08f ok\n"
            "file /work/proj/rich/console.py line 42 value 3.14159\n"
            "commit 9d8f9a372cc5916fd4781fec207ced7ddac2f08f again\n"
            "id 93b4bea6-345f-4c05-a1fc-65d2b3eef08e")
    facts = extract_factsheet(text)
    assert facts.count("9d8f9a372cc5916fd4781fec207ced7ddac2f08f") == 1
    assert "/work/proj/rich/console.py" in facts and "93b4bea6-345f-4c05-a1fc-65d2b3eef08e" in facts
    assert "3.14159" in facts


def test_factsheet_is_capped():
    text = "\n".join(f"/tmp/file_{i:04d}.txt" for i in range(1000))
    assert len(extract_factsheet(text, max_items=50)) == 50


def test_apply_returns_png_images_plus_factsheet_text():
    res = ImageTransform().run(blk(LOG), Label("tool_result:log"), Context())
    imgs = [c for c in res.content if c["type"] == "image"]
    texts = [c for c in res.content if c["type"] == "text"]
    assert imgs and texts and not res.lossless
    assert imgs[0]["source"]["media_type"] == "image/png"
    png = base64.b64decode(imgs[0]["source"]["data"])
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert "rendered as" in texts[-1]["text"] and "verbatim" in texts[-1]["text"]


def test_png_size_reads_header():
    res = ImageTransform().run(blk(LOG), Label("tool_result:log"), Context())
    img = next(c for c in res.content if c["type"] == "image")
    w, h = png_size(img["source"]["data"])
    assert image_tokens(w, h) > 0 and w > 0 and h > 0


def test_declines_short_text_edit_targets_and_errors():
    t = ImageTransform()
    assert t.run(blk("short output"), Label("tool_result:command_output"), Context()) is None
    edit = Label("tool_result:file_read", risks={"code_edit_target"})
    assert t.run(blk(LOG, "Read", {"file_path": "/w/a.py"}), edit, Context()) is None
    err = Block(("messages", 3, "content", 0), "user", "tool_result", LOG, msg_index=3, tool_name="Bash", is_error=True)
    assert t.run(err, Label("tool_result:error"), Context()) is None  # the API rejects images in error results


def test_declines_non_tool_results():
    b = Block(("messages", 0, "content", 0), "user", "text", LOG, msg_index=0)
    assert ImageTransform().run(b, Label("user_prompt"), Context()) is None


def test_non_latin1_characters_do_not_crash_rendering():
    text = "\n".join(f"✓ step {i} done — café ünïcode 🚀 →" for i in range(400))
    res = ImageTransform().run(blk(text), Label("tool_result:log"), Context())
    assert res is not None and any(c["type"] == "image" for c in res.content)
