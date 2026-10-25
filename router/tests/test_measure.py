import json

from reprouter.measure import load_conversations, run_scoreboard
from reprouter.tokens import TokenCounter
from reprouter.transforms.base import Result, Transform


class Halve(Transform):
    name = "halve_m"
    lossless = True

    def applicable(self, block, label, ctx):
        return block.type == "tool_result"

    def apply(self, block, label, ctx):
        return Result(content=block.text[: len(block.text) // 2], lossless=True)


def write_transcript(d):
    proj = d / "-proj"
    proj.mkdir(parents=True)
    recs = [
        {"type": "api-request-shape", "shapeHash": "h", "shape": {
            "system": [{"type": "text", "text": "Primary working directory: /w\nYou are Claude Code."}],
            "tools": [{"name": "Bash", "description": "run", "input_schema": {"type": "object"}}]}},
        {"type": "user", "message": {"role": "user", "content": "please run it"}},
        {"type": "assistant", "message": {"id": "m1", "role": "assistant", "content": [
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}]}},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "z" * 800}]}},
        {"type": "assistant", "message": {"id": "m2", "role": "assistant", "content": [{"type": "text", "text": "done"}]}},
    ]
    (proj / "sess1.jsonl").write_text("\n".join(json.dumps(r) for r in recs))


class FakeCount:
    def __init__(self):
        self.calls = 0

    def __call__(self, body):
        self.calls += 1
        msgs = body["messages"]
        text = json.dumps(body.get("system", "")) + json.dumps(body.get("tools", [])) + json.dumps(msgs)
        return 5 + len(text) // 4


def test_load_conversations_and_scoreboard_end_to_end(tmp_path):
    write_transcript(tmp_path / "projects")
    convs = load_conversations(tmp_path / "projects")
    assert len(convs) == 1 and convs[0]["body"]["messages"][0]["role"] == "user"
    fake = FakeCount()
    counter = TokenCounter(fake, cache_path=tmp_path / "counts.jsonl", model="claude-haiku-5-5")
    out = tmp_path / "results"
    sb = run_scoreboard(convs, [Halve()], counter, out_dir=out, transcripts_root=tmp_path / "projects",
                        fidelity={}, lossless_exempt={"halve_m"})
    assert sb.transforms["halve_m"].applied == 1
    md = (out / "scoreboard.md").read_text()
    assert "| halve_m |" in md
    defaults = json.loads((out / "defaults.json").read_text())
    assert defaults["enabled"] == ["halve_m"]
    calls = fake.calls
    run_scoreboard(convs, [Halve()], counter, out_dir=out, transcripts_root=tmp_path / "projects",
                   fidelity={}, lossless_exempt={"halve_m"})
    assert fake.calls == calls  # second run is fully cached
