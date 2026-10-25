import io
import json

from reprouter.hook import handle_post_tool_use, main
from reprouter.router import Policy
from reprouter.transforms.base import Result, Transform

PYTEST = "\n".join([f"tests/test_x.py::test_{i} PASSED" for i in range(200)]
                   + ["FAILED tests/test_x.py::test_7 - AssertionError", "1 failed, 199 passed in 3.2s"])


class DropPassed(Transform):
    name = "drop_passed"
    lossless = False

    def applicable(self, block, label, ctx):
        return block.type == "tool_result" and "PASSED" in block.text

    def apply(self, block, label, ctx):
        return Result(content="\n".join(l for l in block.text.splitlines() if "PASSED" not in l), lossless=False)


def payload(tool="Bash", stdout=PYTEST, stderr=""):
    return {"session_id": "s1", "transcript_path": "/nonexistent.jsonl", "cwd": "/work/proj",
            "hook_event_name": "PostToolUse", "tool_name": tool, "tool_input": {"command": "pytest"},
            "tool_response": {"stdout": stdout, "stderr": stderr, "interrupted": False, "isImage": False,
                              "noOutputExpected": False}}


POLICY = Policy(transforms=[DropPassed()])


def test_bash_output_replaced_with_same_shape(tmp_path):
    out = handle_post_tool_use(payload(), POLICY, kill_file=tmp_path / "off")
    hso = out["hookSpecificOutput"]
    assert hso["hookEventName"] == "PostToolUse"
    upd = hso["updatedToolOutput"]
    assert set(upd) == set(payload()["tool_response"])
    assert "PASSED" not in upd["stdout"] and "FAILED tests/test_x.py::test_7" in upd["stdout"]
    assert upd["interrupted"] is False


def test_stderr_is_preserved(tmp_path):
    out = handle_post_tool_use(payload(stderr="warning: x"), POLICY, kill_file=tmp_path / "off")
    assert out["hookSpecificOutput"]["updatedToolOutput"]["stderr"] == "warning: x"


def test_other_tools_untouched(tmp_path):
    assert handle_post_tool_use(payload(tool="Read"), POLICY, kill_file=tmp_path / "off") is None


def test_no_change_means_no_output(tmp_path):
    assert handle_post_tool_use(payload(stdout="all good"), POLICY, kill_file=tmp_path / "off") is None


def test_kill_file_disables(tmp_path):
    (tmp_path / "off").write_text("")
    assert handle_post_tool_use(payload(), POLICY, kill_file=tmp_path / "off") is None


def test_main_is_silent_and_exits_zero_on_garbage(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    assert main([]) == 0
    assert capsys.readouterr().out == ""


def test_main_prints_json_for_a_routed_result(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload())))
    monkeypatch.setattr("reprouter.hook.load_policy", lambda: POLICY)
    monkeypatch.setenv("REPROUTER_KILL_FILE", str(tmp_path / "off"))
    monkeypatch.setenv("REPROUTER_HOOK_LOG", str(tmp_path / "hook.jsonl"))
    assert main([]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert "updatedToolOutput" in printed["hookSpecificOutput"]
    log = (tmp_path / "hook.jsonl").read_text()
    assert "drop_passed" in log and "PASSED" not in log  # decisions logged, content never


def test_mode_off_disables(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload())))
    monkeypatch.setattr("reprouter.hook.load_policy", lambda: POLICY)
    monkeypatch.setenv("REPROUTER_MODE", "off")
    monkeypatch.setenv("REPROUTER_KILL_FILE", str(tmp_path / "off"))
    assert main([]) == 0
    assert capsys.readouterr().out == ""
