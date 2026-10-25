import pytest

from reprouter.fidelity.run import build_body, run_probes, transformed_content
from reprouter.fidelity.samples import dedup_samples, json_record_samples
from reprouter.spend import BudgetExceeded, Ledger
from reprouter.transforms.base import Result, Transform


class DropHalf(Transform):
    """Keeps only the first half of the output (loses facts on purpose)."""
    name = "drop_half"
    lossless = False

    def applicable(self, block, label, ctx):
        return block.type == "tool_result"

    def apply(self, block, label, ctx):
        return Result(content=block.text[: len(block.text) // 2], lossless=False)


class FakeHaiku:
    """Answers correctly iff the answer is visible in the last tool_result."""

    def __init__(self):
        self.calls = []

    def __call__(self, body):
        self.calls.append(body)
        last = body["messages"][-1]["content"]
        tr = next(c for c in last if c["type"] == "tool_result")
        ctx = tr["content"] if isinstance(tr["content"], str) else " ".join(x.get("text", "") for x in tr["content"])
        for m in body["messages"][:-1]:  # earlier tool results count too (dedup references)
            for c in m["content"] if isinstance(m["content"], list) else []:
                if c.get("type") == "tool_result":
                    ctx += " " + (c["content"] if isinstance(c["content"], str) else "")
        q = next(c for c in last if c["type"] == "text")["text"]
        ans, full_tail = self.answers.get(q.split("\n")[0], ("NOT PRESENT", ""))
        # computed answers (sums, counts) need the whole output; verbatim ones need the string itself
        visible = ans in ctx or ans == "NOT PRESENT" or (full_tail and full_tail in ctx)
        return (ans if visible else "NOT PRESENT"), {"input_tokens": 1000, "output_tokens": 5}

    answers: dict = {}


def test_build_body_embeds_prior_and_current_results():
    s = dedup_samples(n=1, seed=1)[0]
    body = build_body(s, s.text, s.probes[0].question)
    msgs = body["messages"]
    kinds = [[c["type"] for c in m["content"]] if isinstance(m["content"], list) else ["text"] for m in msgs]
    assert kinds == [["text"], ["tool_use"], ["tool_result"], ["tool_use"], ["tool_result", "text"]]
    assert msgs[-1]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert "NOT PRESENT" in body["system"] and body["tool_choice"] == {"type": "none"}
    assert body["thinking"] == {"type": "disabled"}
    assert msgs[1]["content"][0]["id"] == msgs[2]["content"][0]["tool_use_id"]


def test_transformed_content_uses_only_the_given_transform():
    s = json_record_samples(n=1, seed=1)[0]
    content = transformed_content(s, DropHalf())
    assert isinstance(content, str) and len(content) < len(s.text)


def test_run_probes_pairs_original_and_transformed(tmp_path):
    samples = json_record_samples(n=2, seed=4)
    fake = FakeHaiku()
    FakeHaiku.answers = {p.question: (p.answer, s.text[-40:]) for s in samples for p in s.probes}
    led = Ledger(tmp_path / "l.jsonl", cap_usd=1.0)
    rows = run_probes(samples, [DropHalf()], fake, ledger=led, model="claude-haiku-5-5")
    assert len(rows) == sum(len(s.probes) for s in samples)
    assert all(r["orig_ok"] for r in rows)
    assert any(not r["tx_ok"] for r in rows)  # dropping half loses some facts
    # original answers are computed once per probe, not once per transform
    assert len(fake.calls) == 2 * len(rows)
    assert led.total() > 0


def test_budget_is_checked_before_any_call(tmp_path):
    samples = json_record_samples(n=2, seed=4)
    fake = FakeHaiku()
    led = Ledger(tmp_path / "l.jsonl", cap_usd=0.0000001)
    with pytest.raises(BudgetExceeded):
        run_probes(samples, [DropHalf()], fake, ledger=led, model="claude-haiku-5-5")
    assert fake.calls == []
