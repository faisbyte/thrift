"""Benchmark plumbing: stream parsing, judging and the report (no API calls)."""

import json
import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parents[1] / "bench"
sys.path.insert(0, str(BENCH))

from common import Task, judge_answer, load_suite  # noqa: E402
from parse_stream import parse_stream  # noqa: E402
import report  # noqa: E402

STREAM = [
    {"type": "system", "subtype": "init", "model": "claude-sonnet", "tools": ["Read", "mcp__plugin_thrift_thrift__search_code"],
     "mcp_servers": [{"name": "plugin:thrift:thrift", "status": "connected"}], "plugins": [{"name": "thrift", "path": "/p"}]},
    {"type": "assistant", "message": {"id": "m1", "content": [
        {"type": "tool_use", "id": "t1", "name": "mcp__plugin_thrift_thrift__search_code", "input": {"query": "x"}},
    ]}},
    {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "a" * 100}]}},
    {"type": "assistant", "message": {"id": "m2", "content": [
        {"type": "tool_use", "id": "t2", "name": "Read", "input": {"file_path": "/w/a.py", "offset": 10, "limit": 20}},
        {"type": "tool_use", "id": "t3", "name": "Read", "input": {"file_path": "/w/b.py"}},
        {"type": "tool_use", "id": "t4", "name": "Read", "input": {"file_path": "/w/a.py"}},
    ]}},
    {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "t2", "content": [{"type": "text", "text": "b" * 50}]},
        {"type": "tool_result", "tool_use_id": "t3", "content": "c" * 70},
    ]}},
    {"type": "assistant", "parent_tool_use_id": "sub", "message": {"content": [
        {"type": "tool_use", "id": "t9", "name": "Read", "input": {"file_path": "/w/ignored.py"}}]}},
    {"type": "result", "subtype": "success", "is_error": False, "num_turns": 3, "duration_ms": 1234,
     "total_cost_usd": 0.05, "result": "It is in console.py: _detect_color_system",
     "usage": {"input_tokens": 10, "cache_creation_input_tokens": 1000, "cache_read_input_tokens": 5000,
               "output_tokens": 300},
     "modelUsage": {"claude-sonnet": {"inputTokens": 10, "outputTokens": 300, "cacheReadInputTokens": 5000,
                                      "cacheCreationInputTokens": 1000, "costUSD": 0.05}}},
]


def test_parse_stream():
    m = parse_stream(json.dumps(e) for e in STREAM)
    assert m["input_total"] == 6010 and m["output_tokens"] == 300 and m["cost_usd"] == 0.05
    assert m["thrift_calls"] == 1 and m["reads"] == 3 and m["reads_ranged"] == 1
    assert m["files_read"] == 2  # unique, subagent read ignored
    assert m["tool_result_chars"] == {"mcp__plugin_thrift_thrift__search_code": 100, "Read": 120}
    assert m["init_plugins"][0]["name"] == "thrift"
    assert m["answer"].startswith("It is in console.py")


def test_judge_answer():
    t = Task(id="x", kind="qa", prompt="p", expect_all=[r"console\.py", "_detect_color_system"])
    assert judge_answer(t, "see rich/console.py, Console._detect_color_system")
    assert not judge_answer(t, "see rich/color.py")
    t2 = Task(id="y", kind="qa", prompt="p", expect_any=["a", "b"])
    assert judge_answer(t2, "b") and not judge_answer(t2, "c")


def test_tasks_file_loads():
    suite = load_suite(BENCH / "tasks.yaml")
    ids = [t.id for t in suite.tasks]
    assert len(ids) == len(set(ids)) >= 15
    for t in suite.tasks:
        assert t.kind in ("qa", "edit")
        if t.kind == "edit":
            assert t.setup and t.check
        else:
            assert t.expect_all or t.expect_any


def test_report_paired_ratios(tmp_path):
    runs = []
    for task in ("t1", "t2", "t3"):
        for rep in range(3):
            runs.append({"key": f"{task}__baseline__{rep}", "task": task, "kind": "qa", "split": "dev",
                         "arm": "baseline", "rep": rep, "success": True, "cost_usd": 0.10, "input_total": 10000,
                         "output_tokens": 500, "wall_s": 30, "num_turns": 8, "files_read": 6,
                         "tool_result_chars_total": 40000, "reads": 6, "reads_ranged": 0})
            runs.append({"key": f"{task}__thrift__{rep}", "task": task, "kind": "qa", "split": "dev",
                         "arm": "thrift", "rep": rep, "success": rep != 0, "cost_usd": 0.05, "input_total": 6000,
                         "output_tokens": 400, "wall_s": 20, "num_turns": 5, "files_read": 2,
                         "tool_result_chars_total": 10000, "reads": 2, "reads_ranged": 1, "thrift_calls": 2,
                         "hook_fired": 1})
    (tmp_path / "runs.jsonl").write_text("\n".join(json.dumps(r) for r in runs))
    p = report.paired(runs, "thrift")
    g, lo, hi, n = p["summary"]["cost_usd"]
    assert n == 3 and abs(g - 0.5) < 1e-9
    md = report.render(runs, {"arms": ["baseline", "thrift"], "repo": {"name": "rich", "sha": "abc"}})
    assert "| cost $ | -50% |" in md
    assert "| thrift | 9 | 67% |" in md
