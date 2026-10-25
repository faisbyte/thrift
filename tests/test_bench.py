"""Benchmark plumbing: stream parsing, judging and the report (no API calls)."""

import json
import pytest
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
            assert (t.setup or t.reference) and t.check  # an injected bug, or a reference solution
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


# --------------------------------------------------------------------- phase 3: auth, pricing, task selection

import pricing  # noqa: E402
import run as bench_run  # noqa: E402


def test_price_lookup():
    assert pricing.price_for("claude-haiku-5-5") == (0.10, 0.50, 0.01)
    assert pricing.price_for("claude-sonnet-5-5") == (2.00, 10.00, 0.10)
    assert pricing.price_for("claude-sonnet-5") == (2.00, 10.00, 0.20)  # not confused with 5-5
    assert pricing.price_for("claude-opus-5-5[1m]") == (4.00, 20.00, 0.20)
    assert pricing.price_for("haiku") == pricing.PRICES["claude-haiku-5-5"]
    assert pricing.price_for("gpt-4") is None


def test_estimate_cost_matches_hand_calculation():
    usage = {"claude-sonnet-5-5": {"inputTokens": 2, "outputTokens": 8, "cacheReadInputTokens": 12744,
                                   "cacheCreationInputTokens": 3485}}
    # 2*2 + 3485*2*1.25 + 12744*0.10 + 8*10 = 10070.5 per million (matches Claude Code's own figure)
    assert pricing.estimate_cost(usage) == 0.010071
    # all writes at the 1-hour TTL cost 2x input instead of 1.25x
    assert pricing.estimate_cost(usage, cache_1h_fraction=1.0) == round((4 + 3485 * 4 + 1274.4 + 80) / 1e6, 6)
    assert pricing.estimate_cost({"mystery-model": {"inputTokens": 1}}) is None
    assert pricing.estimate_cost({}) == 0.0


def test_resolve_auth_api_key_never_exposes_key():
    secret = "sk-ant-api03-SECRETSECRET"
    auth, extra, env = bench_run.resolve_auth("auto", "isolated", {"ANTHROPIC_API_KEY": secret,
                                                                   "CLAUDE_CODE_OAUTH_TOKEN": "tok-SECRET"})
    assert auth == "api-key"
    assert secret not in " ".join(extra) and "$ANTHROPIC_API_KEY" in " ".join(extra)
    assert env == {"CLAUDE_CODE_OAUTH_TOKEN": None}  # key wins; token removed from the child


def test_resolve_auth_subscription_default_without_key():
    token = "sk-ant-oat01-SECRETTOKEN"
    auth, extra, env = bench_run.resolve_auth("auto", "isolated", {"CLAUDE_CODE_OAUTH_TOKEN": token})
    assert auth == "subscription"
    assert extra == []  # nothing on the command line
    assert env["ANTHROPIC_API_KEY"] is None and env["CLAUDE_CODE_OAUTH_TOKEN"] == token


def test_resolve_auth_errors():
    with pytest.raises(SystemExit, match="claude setup-token"):
        bench_run.resolve_auth("auto", "isolated", {})
    with pytest.raises(SystemExit, match="bare"):
        bench_run.resolve_auth("subscription", "bare", {"CLAUDE_CODE_OAUTH_TOKEN": "t"})
    with pytest.raises(SystemExit, match="ANTHROPIC_API_KEY"):
        bench_run.resolve_auth("api-key", "isolated", {})
    # --mode user + subscription can fall back to the normal login (no token needed)
    assert bench_run.resolve_auth("subscription", "user", {})[0] == "subscription"


def test_select_tasks_by_kind_and_id():
    suite = load_suite(BENCH / "tasks.yaml")
    edit = bench_run.select_tasks(suite, "edit", None, None, False)
    assert edit and all(t.kind == "edit" for t in edit)
    mixed = bench_run.select_tasks(suite, "edit,qa-wrap", None, None, False)
    assert {t.kind for t in mixed} == {"edit", "qa"} and len(mixed) == len(edit) + 1
    assert len(bench_run.select_tasks(suite, "all", None, None, False)) == len(suite.tasks)
    with pytest.raises(SystemExit):
        bench_run.select_tasks(suite, "edits", None, None, False)


def test_run_cost_basis():
    assert bench_run.run_cost({"cost_basis": "reported", "cost_usd": 0.5, "est_cost_usd": 0.4}) == 0.5
    assert bench_run.run_cost({"cost_basis": "estimated", "cost_usd": 0.5, "est_cost_usd": 0.4}) == 0.4
    assert bench_run.run_cost({}) == 0.0


def test_parse_stream_cache_ttl_and_prompt_size():
    events = [
        {"type": "assistant", "message": {"id": "a", "content": [], "usage": {
            "input_tokens": 5, "cache_read_input_tokens": 1000, "cache_creation_input_tokens": 300,
            "cache_creation": {"ephemeral_5m_input_tokens": 100, "ephemeral_1h_input_tokens": 200}}}},
        {"type": "assistant", "message": {"id": "a", "content": [], "usage": {  # same response, counted once
            "input_tokens": 5, "cache_read_input_tokens": 1000, "cache_creation_input_tokens": 300,
            "cache_creation": {"ephemeral_5m_input_tokens": 100, "ephemeral_1h_input_tokens": 200}}}},
        {"type": "result", "usage": {}, "modelUsage": {"claude-haiku-5-5": {"inputTokens": 5}}},
    ]
    m = parse_stream(json.dumps(e) for e in events)
    assert m["cache_write_1h_fraction"] == round(200 / 300, 4)
    assert m["max_prompt_tokens"] == 1305
    assert "claude-haiku-5-5" in m["model_usage"]


def test_report_uses_estimated_cost_for_subscription_runs():
    runs = [_run("t", "edit", arm, 0, True, 1.0) for arm in ("baseline", "thrift")]
    runs[0].update(cost_basis="estimated", est_cost_usd=0.02)
    runs[1].update(cost_basis="estimated", est_cost_usd=0.01)
    md = report.render(runs, {"arms": ["baseline", "thrift"], "auth": "subscription"})
    assert "estimated API prices" in md and "| Edit tasks | 1 | 1/1 | -50% |" in md


# --------------------------------------------------------------------- reprouter arms

def test_router_arms_route_through_the_proxy():
    assert bench_run.ARMS["router"]["proxy"] == "on" and bench_run.ARMS["router"]["canary"] is True
    assert bench_run.ARMS["base-proxy"]["proxy"] == "record"
    assert bench_run.ARMS["router-hook"]["hook"] is True


def test_proxy_cmd(tmp_path):
    cmd = bench_run.proxy_cmd(47999, "on", tmp_path, canary=True)
    assert cmd[1:5] == ["-m", "reprouter.cli", "proxy", "--port"] and "47999" in cmd
    assert cmd[cmd.index("--mode") + 1] == "on" and "--canary" in cmd
    assert cmd[cmd.index("--state-file") + 1] == str(tmp_path / "proxy-state.json")
    assert "--canary" not in bench_run.proxy_cmd(47999, "record", tmp_path, canary=False)


def test_merge_settings_keeps_api_key_helper_and_adds_hooks():
    cmd = ["claude", "-p", "x", "--settings", json.dumps({"apiKeyHelper": 'printf %s "$ANTHROPIC_API_KEY"'})]
    out = bench_run.merge_settings(cmd, bench_run.hook_settings("/py/bin/python"))
    s = json.loads(out[out.index("--settings") + 1])
    assert s["apiKeyHelper"].startswith("printf")
    hook = s["hooks"]["PostToolUse"][0]
    assert hook["matcher"] == "Bash" and "reprouter.hook" in hook["hooks"][0]["command"]
    assert out.count("--settings") == 1
    out2 = bench_run.merge_settings(["claude", "-p", "x"], {"a": 1})
    assert json.loads(out2[out2.index("--settings") + 1]) == {"a": 1}


def test_summarize_proxy_corpus(tmp_path):
    d = tmp_path / "proxy"
    d.mkdir()
    rows = [{"status": 200, "request_class": "main", "decisions": [{"transform": "log_filter", "before": 900, "after": 100}]},
            {"status": 400, "request_class": "main", "decisions": []},
            {"status": 200, "request_class": "auxiliary", "decisions": []}]
    (d / "20261025-abc.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
    s = bench_run.summarize_proxy_corpus(d)
    assert s == {"proxy_requests": 3, "proxy_errors": 1, "router_blocks": 1, "router_saved_est": 800,
                 "router_transforms": {"log_filter": 1}}
    assert bench_run.summarize_proxy_corpus(tmp_path / "missing")["proxy_requests"] == 0


# --------------------------------------------------------------------- setup ops for heavier tasks

def test_apply_setup_create_and_run_ops(tmp_path):
    from common import apply_setup

    (tmp_path / "a.py").write_text("x = 1\n")
    apply_setup(tmp_path, [
        {"file": "a.py", "old": "x = 1", "new": "x = 2"},
        {"create": "pkg/new.py", "content": "VALUE = 3\n"},
        {"run": "{python} -c \"open('gen.txt','w').write('generated')\""},
    ])
    assert (tmp_path / "a.py").read_text() == "x = 2\n"
    assert (tmp_path / "pkg" / "new.py").read_text() == "VALUE = 3\n"
    assert (tmp_path / "gen.txt").read_text() == "generated"


def test_failing_run_op_raises(tmp_path):
    from common import apply_setup

    with pytest.raises(Exception):
        apply_setup(tmp_path, [{"run": "{python} -c \"raise SystemExit(3)\""}])


def test_task_accepts_reference_solution():
    from common import Task

    t = Task(id="x", kind="edit", prompt="p", check="true", reference=[{"create": "a.py", "content": "1"}])
    assert t.reference[0]["create"] == "a.py"


# --------------------------------------------------------------------- smoke-run plumbing

def test_router_smoke_arms_have_fixed_transform_sets():
    assert bench_run.ARMS["router-elide"]["transforms"] == "tool_input_elide"
    safe = bench_run.ARMS["router-safe"]["transforms"].split(",")
    assert safe[0] == "tool_input_elide" and {"log_filter", "whitespace", "json_minify", "toon", "csv_table"} <= set(safe)
    assert bench_run.ARMS["router-elide"]["canary"] and bench_run.ARMS["router-safe"]["canary"]


def test_summarize_proxy_corpus_counts_final_request_not_cumulative(tmp_path):
    d = tmp_path / "proxy"
    d.mkdir()
    dec = lambda n: [{"transform": "tool_input_elide", "before": 1000, "after": 100, "path": [3, i]} for i in range(n)]
    rows = [{"status": 200, "request_class": "main", "session": "s", "decisions": dec(1)},
            {"status": 200, "request_class": "main", "session": "s", "decisions": dec(2)},
            {"status": 200, "request_class": "main", "session": "s", "decisions": dec(2)}]
    (d / "x.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
    s = bench_run.summarize_proxy_corpus(d)
    assert s["router_blocks"] == 2 and s["router_saved_est"] == 1800  # the last request carries every routed block
    assert s["router_transforms"] == {"tool_input_elide": 2} and s["proxy_requests"] == 3


def test_reads_after_write_counts_rereads_of_files_the_model_wrote():
    from parse_stream import parse_stream

    def use(i, name, inp):
        return {"type": "assistant", "message": {"id": f"m{i}", "content": [
            {"type": "tool_use", "id": f"t{i}", "name": name, "input": inp}]}}

    ev = [use(1, "Write", {"file_path": "/w/rich/_stats.py", "content": "x"}),
          use(2, "Read", {"file_path": "/w/rich/other.py"}),
          use(3, "Read", {"file_path": "/w/rich/_stats.py"}),
          use(4, "Edit", {"file_path": "/w/rich/other.py", "old_string": "a", "new_string": "b"}),
          use(5, "Bash", {"command": "cat rich/other.py"}),
          use(6, "Read", {"file_path": "/w/rich/_stats.py", "offset": 3}),
          {"type": "result", "usage": {}, "modelUsage": {}}]
    m = parse_stream(json.dumps(e) for e in ev)
    assert m["reads_after_write"] == 3  # _stats.py twice, other.py once via cat; the first other.py read came before its Edit


def test_per_run_budget_splits_what_is_left_of_max_cost():
    assert bench_run.per_run_budget(2.0, None, 0.0, 3) == 2.0
    assert bench_run.per_run_budget(2.0, 0.10, 0.01, 3) == pytest.approx(0.03)
    assert bench_run.per_run_budget(0.02, 0.10, 0.0, 1) == 0.02


def test_record_bench_spend_writes_billed_cost_to_the_project_ledger(tmp_path):
    led = tmp_path / "ledger.jsonl"
    bench_run.record_bench_spend(led, {"key": "t__router-elide__0", "cost_usd": 0.012}, "api-key")
    bench_run.record_bench_spend(led, {"key": "t__baseline__0", "est_cost_usd": 0.5,
                                       "cost_basis": "estimated"}, "subscription")
    from reprouter.spend import Ledger
    L = Ledger(led, cap_usd=3.0)
    assert L.total() == pytest.approx(0.012) and L.unbilled_total() == pytest.approx(0.5)
