import pytest

from reprouter.scoreboard import REGIMES, iter_tool_results, measure, render_markdown, verdicts
from reprouter.transforms.base import Result, Transform


class Halve(Transform):
    name = "halve"
    lossless = True

    def applicable(self, block, label, ctx):
        return block.type == "tool_result" and block.tool_name == "Bash"

    def apply(self, block, label, ctx):
        return Result(content=block.text[: len(block.text) // 2], lossless=True)


class Never(Transform):
    name = "never"
    lossless = True

    def applicable(self, block, label, ctx):
        return False

    def apply(self, block, label, ctx):
        return None


def conv(n_turns_after=2):
    msgs = [{"role": "user", "content": [{"type": "text", "text": "go"}]},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "a", "name": "Bash", "input": {"command": "x"}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "a", "content": "y" * 400}]}]
    for i in range(n_turns_after):
        msgs.append({"role": "assistant", "content": [{"type": "text", "text": f"turn {i}"}]})
        msgs.append({"role": "user", "content": [{"type": "text", "text": "ok"}]})
    return {"session": "s", "body": {"system": [{"type": "text", "text": "cwd: /w"}], "messages": msgs}}


def count(text):
    return len(text) // 4


def test_iter_tool_results_reports_turns_after():
    recs = list(iter_tool_results([conv(3)]))
    assert len(recs) == 1
    r = recs[0]
    assert r.block.tool_name == "Bash" and r.turns_after == 3


def test_measure_weights_savings_by_cache_regime():
    sb = measure([conv(3)], [Halve(), Never()], count=count, prefix_tokens=1000)
    h = sb.transforms["halve"]
    assert h.applied == 1 and h.tokens_before == 100 and h.tokens_after == 50
    api = REGIMES["api_5m_haiku"]
    assert h.weighted["api_5m_haiku"] == pytest.approx(50 * (api.write + api.read * 3))
    assert sb.transforms["never"].applied == 0
    assert sb.tool_result_tokens == 100
    # session cost includes the fixed prefix (written once, read on later turns) and every block
    assert sb.session_weighted["api_5m_haiku"] > h.weighted["api_5m_haiku"]


def test_measure_by_kind_breakdown():
    sb = measure([conv(1)], [Halve()], count=count, prefix_tokens=0)
    assert sb.transforms["halve"].by_kind["tool_result:command_output"]["saved"] == 50


def test_verdicts_require_savings_and_fidelity():
    sb = measure([conv(3)], [Halve(), Never()], count=count, prefix_tokens=1000)
    fid = {"halve": {"retained": 0.99, "n_paired": 40}}
    v = verdicts(sb, fid, min_share=0.0001, min_retained=0.95)
    assert v["halve"][0] is True
    assert v["never"][0] is False and "no savings" in v["never"][1]
    v2 = verdicts(sb, {"halve": {"retained": 0.80, "n_paired": 40}}, min_share=0.0001, min_retained=0.95)
    assert v2["halve"][0] is False and "fidelity" in v2["halve"][1]
    v3 = verdicts(sb, {}, min_share=0.0001, min_retained=0.95)
    assert v3["halve"][0] is False and "untested" in v3["halve"][1]


def test_lossless_transforms_do_not_need_probes():
    sb = measure([conv(3)], [Halve()], count=count, prefix_tokens=1000)
    v = verdicts(sb, {}, min_share=0.0001, min_retained=0.95, lossless_exempt={"halve"})
    assert v["halve"][0] is True


def test_render_markdown_contains_table_rows():
    sb = measure([conv(3)], [Halve()], count=count, prefix_tokens=1000)
    md = render_markdown(sb, {"halve": {"retained": 1.0, "n_paired": 3}}, verdicts(sb, {}, lossless_exempt={"halve"}))
    assert "| halve |" in md and "api_5m_haiku" in md


def test_verdict_uses_promised_retention_when_available():
    sb = measure([conv(3)], [Halve()], count=count, prefix_tokens=1000)
    fid = {"halve": {"retained": 0.5, "n_paired": 40, "promised": {"retained": 0.98, "n_paired": 20}}}
    assert verdicts(sb, fid, min_share=0.0001, min_retained=0.95)["halve"][0] is True


def test_measure_includes_assistant_tool_use_blocks():
    from reprouter.transforms.tool_input_elide import ToolInputElide

    c = conv(3)
    big = "\n".join(f"line {i} of the new file" for i in range(100))
    c["body"]["messages"][1]["content"] = [{"type": "tool_use", "id": "a", "name": "Write",
                                            "input": {"file_path": "/w/x.py", "content": big}}]
    sb = measure([c], [ToolInputElide()], count=count, prefix_tokens=0)
    st = sb.transforms["tool_input_elide"]
    assert st.applied == 1 and st.tokens_after < st.tokens_before
    assert "tool_use" in st.by_kind
