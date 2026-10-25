import copy
import json

from rr_fixtures import claude_code_request
from reprouter.router import Policy, route
from reprouter.transforms.base import Result, Transform

PYTEST_LONG = "\n".join(["tests/test_x.py::test_%d PASSED" % i for i in range(200)]
                        + ["FAILED tests/test_x.py::test_7 - AssertionError", "1 failed, 199 passed in 3.2s"])


class Squash(Transform):
    """Lossless stand-in: removes 'PASSED' rows (pretend reversible)."""
    name = "squash"
    lossless = True

    def applicable(self, block, label, ctx):
        return block.type == "tool_result" and "PASSED" in block.text

    def apply(self, block, label, ctx):
        kept = [ln for ln in block.text.splitlines() if "PASSED" not in ln]
        return Result(content="\n".join(kept), lossless=True)


class Tiny(Transform):
    """Saves almost nothing: strips one trailing character."""
    name = "tiny"
    lossless = True

    def applicable(self, block, label, ctx):
        return block.type == "tool_result" and len(block.text) > 1

    def apply(self, block, label, ctx):
        return Result(content=block.text[:-1], lossless=True)


class Lossy(Transform):
    name = "lossy"
    lossless = False

    def applicable(self, block, label, ctx):
        return block.type == "tool_result"

    def apply(self, block, label, ctx):
        return Result(content=block.text[:20], lossless=False)


class Picture(Transform):
    name = "picture"
    lossless = False

    def applicable(self, block, label, ctx):
        return block.type == "tool_result"

    def apply(self, block, label, ctx):
        return Result(content=[{"type": "text", "text": "pic"}], lossless=False)


class Boom(Transform):
    name = "boom"
    lossless = True

    def applicable(self, block, label, ctx):
        return True

    def apply(self, block, label, ctx):
        raise RuntimeError("bug")


def req_with_pytest():
    req = claude_code_request()
    req["messages"][4]["content"][0]["content"] = [{"type": "text", "text": PYTEST_LONG}]
    return req


def decision_for(decisions, path):
    return next(d for d in decisions if d.path == path)


def test_no_transforms_returns_identical_request():
    req = req_with_pytest()
    out, decisions = route(req, Policy(transforms=[]))
    assert out == req
    assert all(d.transform is None for d in decisions)


def test_applies_lossless_transform_that_saves_enough():
    out, decisions = route(req_with_pytest(), Policy(transforms=[Squash()]))
    tr = out["messages"][4]["content"][0]
    assert "PASSED" not in tr["content"] and tr["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    d = decision_for(decisions, ("messages", 4, "content", 0))
    assert d.transform == "squash" and d.tokens_after < d.tokens_before


def test_skips_transform_below_margin():
    out, decisions = route(req_with_pytest(), Policy(transforms=[Tiny()], margin=0.10))
    assert out == req_with_pytest()
    d = decision_for(decisions, ("messages", 4, "content", 0))
    assert d.transform is None and "margin" in d.reason


def test_picks_cheapest_candidate():
    _, decisions = route(req_with_pytest(), Policy(transforms=[Tiny(), Squash()], margin=0.0))
    assert decision_for(decisions, ("messages", 4, "content", 0)).transform == "squash"


def test_never_touches_non_tool_result_blocks():
    req = req_with_pytest()
    out, _ = route(req, Policy(transforms=[Lossy()], margin=0.0))
    assert out["system"] == req["system"] and out["tools"] == req["tools"]
    assert out["messages"][0] == req["messages"][0] and out["messages"][1] == req["messages"][1]


def test_lossy_transforms_skip_edit_targets():
    req = req_with_pytest()  # messages[2] is a Read of /work/proj/m.py inside the project
    out, decisions = route(req, Policy(transforms=[Lossy()], margin=0.0))
    assert out["messages"][2] == req["messages"][2]
    assert "exact" in decision_for(decisions, ("messages", 2, "content", 0)).reason
    assert out["messages"][4] != req["messages"][4]  # the pytest output has no exact risks


def test_non_text_results_disabled_unless_policy_allows():
    req = req_with_pytest()
    out, _ = route(req, Policy(transforms=[Picture()], margin=0.0))
    assert out == req
    out, _ = route(req, Policy(transforms=[Picture()], margin=0.0, allow_nontext=True))
    assert out["messages"][4]["content"][0]["content"] == [{"type": "text", "text": "pic"}]


def test_failing_transform_leaves_block_untouched():
    req = req_with_pytest()
    out, decisions = route(req, Policy(transforms=[Boom()], margin=0.0))
    assert out == req


def test_min_msg_index_protects_history_seen_before_the_router():
    req = req_with_pytest()
    out, _ = route(req, Policy(transforms=[Squash()]), min_msg_index=5)
    assert out == req


def test_decisions_are_byte_stable_across_turns():
    """Turn k+1 resends turn k's messages; their routed bytes must not change."""
    turn1 = req_with_pytest()
    turn2 = copy.deepcopy(turn1)
    turn2["messages"][4]["content"][0].pop("cache_control")  # Claude Code moves the marker forward
    turn2["messages"].append({"role": "assistant", "content": [
        {"type": "tool_use", "id": "tu_3", "name": "Bash", "input": {"command": "pytest -q"}}]})
    turn2["messages"].append({"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "tu_3", "content": PYTEST_LONG,
         "cache_control": {"type": "ephemeral", "ttl": "1h"}}]})
    policy = Policy(transforms=[Squash(), Lossy()], margin=0.0)
    out1, _ = route(turn1, policy)
    out2, _ = route(turn2, policy)
    def strip(m):  # cache markers legitimately move between turns; content must not
        m = copy.deepcopy(m)
        for b in m["content"] if isinstance(m["content"], list) else []:
            b.pop("cache_control", None)
        return json.dumps(m, sort_keys=True)

    for i in range(5):
        assert strip(out1["messages"][i]) == strip(out2["messages"][i]), i


def test_cleanup_stage_chains_before_main_stage():
    class Cleanup(Transform):
        name = "cleanup"
        lossless = True
        stage = "cleanup"

        def applicable(self, block, label, ctx):
            return block.type == "tool_result"

        def apply(self, block, label, ctx):
            return Result(content=block.text.replace("tests/test_x.py::", ""), lossless=True)

    out, decisions = route(req_with_pytest(), Policy(transforms=[Cleanup(), Squash()], margin=0.0))
    text = out["messages"][4]["content"][0]["content"]
    assert "tests/test_x.py::" not in text and "PASSED" not in text
    assert decision_for(decisions, ("messages", 4, "content", 0)).transform == "cleanup+squash"


def test_decide_single_block_matches_route():
    from reprouter.classify import classify_block
    from reprouter.request import Block
    from reprouter.router import decide
    from reprouter.transforms.base import Context

    b = Block(("messages", 4, "content", 0), "user", "tool_result", PYTEST_LONG, msg_index=4,
              tool_name="Bash", tool_input={"command": "pytest"})
    label = classify_block(b, project_root="/work/proj")
    decision, content = decide(b, label, Context(project_root="/work/proj"), Policy(transforms=[Squash()]))
    assert decision.transform == "squash" and "PASSED" not in content
    _, decisions = route(req_with_pytest(), Policy(transforms=[Squash()]))
    assert decision_for(decisions, ("messages", 4, "content", 0)).tokens_after == decision.tokens_after


def test_decide_returns_none_content_when_unchanged():
    from reprouter.classify import classify_block
    from reprouter.request import Block
    from reprouter.router import decide
    from reprouter.transforms.base import Context

    b = Block(("messages", 4, "content", 0), "user", "tool_result", "ok", msg_index=4, tool_name="Bash")
    decision, content = decide(b, classify_block(b), Context(), Policy(transforms=[Squash()]))
    assert decision.transform is None and content is None


def test_router_elides_large_assistant_tool_inputs_byte_stably():
    from reprouter.transforms.tool_input_elide import ToolInputElide

    big = "\n".join(f"row {i} = {i * 7}" for i in range(200))
    req = claude_code_request()
    req["messages"][3]["content"] = [{"type": "tool_use", "id": "tu_2", "name": "Write",
                                      "input": {"file_path": "/work/proj/data.py", "content": big}}]
    out, decisions = route(req, Policy(transforms=[ToolInputElide()]))
    assert "row 150" not in out["messages"][3]["content"][0]["input"]["content"]
    assert out["messages"][3]["content"][0]["input"]["file_path"] == "/work/proj/data.py"
    assert decision_for(decisions, ("messages", 3, "content", 0)).transform == "tool_input_elide"
    out2, _ = route(req, Policy(transforms=[ToolInputElide()]))
    assert out2 == out
    out3, _ = route(req, Policy(transforms=[ToolInputElide()]), min_msg_index=4)
    assert out3["messages"][3] == req["messages"][3]
