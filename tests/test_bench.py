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


def _run(task, kind, arm, rep, correct, cost, inp=10000, out=500, files=5, hook=None, ctx=None, answered=True):
    return {"key": f"{task}__{arm}__{rep}", "task": task, "kind": kind, "split": "dev", "arm": arm, "rep": rep,
            "correct": correct, "success": correct, "answered": answered, "cost_usd": cost,
            "input_tokens": inp // 10, "cache_write_tokens": inp // 2, "cache_read_tokens": inp - inp // 10 - inp // 2,
            "input_total": inp, "output_tokens": out, "wall_s": 30, "num_turns": 6, "files_read": files,
            "hook_gold_hit": hook, "hook_context": ctx}


def _suite_runs():
    runs = []
    for rep in range(2):
        # edit task where the hook pointed at the right file: thrift cheaper, both correct
        runs.append(_run("e-hit", "edit", "baseline", rep, True, 0.20, inp=20000, files=6))
        runs.append(_run("e-hit", "edit", "thrift", rep, True, 0.10, inp=10000, files=2, hook=True, ctx="a.py L1-2 f"))
        # edit task where the hook pointed at the wrong file: thrift worse
        runs.append(_run("e-miss", "edit", "baseline", rep, True, 0.10, inp=10000))
        runs.append(_run("e-miss", "edit", "thrift", rep, rep == 0, 0.15, inp=15000, hook=False, ctx="z.py L1-2 g"))
        # qa task, hook silent (also a miss)
        runs.append(_run("q-1", "qa", "baseline", rep, True, 0.05, inp=5000))
        runs.append(_run("q-1", "qa", "thrift", rep, False, 0.05, inp=5000, hook=False, ctx=None,
                         answered=rep == 0))
    return runs


def test_hook_tags():
    tags = report.task_hook_tags(_suite_runs())
    assert tags == {"e-hit": "hook_hit", "e-miss": "hook_miss", "q-1": "hook_miss"}
    # baseline-only task has no hook data
    assert report.task_hook_tags([_run("b", "qa", "baseline", 0, True, 0.1)]) == {"b": "n/a"}


def test_correct_vs_answered_fallbacks():
    assert report.correct({"success": True}) and not report.correct({"correct": False, "success": True})
    assert not report.answered({"is_error": True, "answer": "x"})
    assert report.answered({"answer": "x"}) and not report.answered({"answer": "  "})


def test_compare_groups():
    runs = _suite_runs()
    edit = report.compare([r for r in runs if r["kind"] == "edit"], "thrift", "cost_usd")
    assert edit["tasks"] == 2 and edit["arm"]["n"] == 4 and edit["base"]["n"] == 4
    # mean cost: thrift (0.10+0.15)/2=0.125 vs base (0.20+0.10)/2=0.15 -> -16.7%
    assert round(edit["cost_change_pct"], 1) == -16.7
    # success: thrift 3/4 vs base 4/4 -> -25 pp
    assert edit["success_change_pp"] == -25.0
    qa = report.compare([r for r in runs if r["kind"] == "qa"], "thrift", "cost_usd")
    assert qa["cost_change_pct"] == 0 and qa["success_change_pp"] == -100.0
    overall = report.compare(runs, "thrift", "cost_usd")
    assert overall["tasks"] == 3


def test_render_sections_and_summary():
    md = report.render(_suite_runs(), {"arms": ["baseline", "thrift"], "repo": {"name": "rich", "sha": "abc"}})
    for heading in ["## Edit tasks: per task", "## Q&A tasks: per task", "## hook_miss tasks", "## Totals per arm",
                    "## All runs", "## Summary"]:
        assert heading in md, heading
    assert md.rstrip().splitlines()[-1].startswith("| hook_miss tasks only")  # ends with the summary
    summary = md[md.index("## Summary"):]
    assert "| Edit tasks | 2 | 4/4 | -17% |" in summary
    assert "| Q&A tasks | 1 | 2/2 | +0% |" in summary and "-100 pp" in summary
    assert "| Overall | 3 | 6/6 |" in summary
    miss = md[md.index("## hook_miss tasks"):md.index("## Totals per arm")]
    assert "e-miss" in miss and "q-1" in miss and "e-hit" not in miss
    runs_tbl = md[md.index("## All runs"):md.index("## Summary")]
    assert "| e-miss__thrift__1 | yes | ✗ |" in runs_tbl and "wrong" in runs_tbl
    assert "| q-1__thrift__1 | NO | ✗ |" in runs_tbl and "silent" in runs_tbl
    # per-task row carries all requested columns (cache read/write, turns, files)
    edit_tbl = md[md.index("## Edit tasks"):md.index("## Q&A tasks")]
    assert "cache write" in edit_tbl and "cache read" in edit_tbl and "files read" in edit_tbl
    assert "| e-hit | hook_hit | baseline | 2 | 100% | 0.2000 |" in edit_tbl


def test_report_cli_writes_files(tmp_path):
    runs = _suite_runs()
    (tmp_path / "runs.jsonl").write_text("\n".join(json.dumps(r) for r in runs))
    sys.argv = ["report.py", str(tmp_path)]
    assert report.main() == 0
    assert (tmp_path / "summary.md").exists()
    rows = (tmp_path / "runs.csv").read_text().splitlines()
    assert len(rows) == len(runs) + 1 and "correct" in rows[0]
