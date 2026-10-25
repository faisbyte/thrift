#!/usr/bin/env python3
"""A/B benchmark: Claude Code headless runs with and without the thrift plugin.

Usage (from the repo root):
  uv run python bench/run.py --validate            # check tasks (bugs fail, fixes pass)
  uv run python bench/run.py --smoke               # verify plugin/MCP/hook load in each arm
  uv run python bench/run.py --quick               # small paired run (~20 runs)
  uv run python bench/run.py --repeats 5           # full run
  uv run python bench/report.py bench/results/<ts> # comparison table

Requires ANTHROPIC_API_KEY, read from the environment or the repo's .env
(default "isolated" mode runs with an empty
CLAUDE_CONFIG_DIR so none of your own settings, plugins, hooks, MCP servers or
CLAUDE.md leak into either arm).
"""

from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

from common import (
    CACHE_DIR,
    PLUGIN_DIR,
    REPO_ROOT,
    Suite,
    Task,
    apply_setup,
    base_checkout,
    bench_env,
    judge_answer,
    load_suite,
    make_workdir,
    run_check,
    test_venv,
)
from parse_stream import THRIFT_TOOL_PREFIX, parse_file

THRIFT_SERVER_RULE = "mcp__plugin_thrift_thrift"

ARMS = {
    "baseline": {"plugin": False},
    "thrift": {"plugin": True},
    "hook-only": {"plugin": True, "disallow": [THRIFT_SERVER_RULE]},
    "mcp-only": {"plugin": True, "env": {"THRIFT_HOOK": "0"}},
}

# Same permissions for every arm. Bash is limited to what the tasks need.
ALLOWED_TOOLS = [
    "Read", "Edit", "Write", "Grep", "Glob",
    "Bash(python -m pytest *)", "Bash(pytest *)", "Bash(git diff *)", "Bash(git status *)",
    "Bash(ls *)", "Bash(cat *)", "Bash(grep *)", "Bash(rg *)", "Bash(find *)", "Bash(head *)", "Bash(sed -n *)",
    THRIFT_SERVER_RULE,
]
DISALLOWED_TOOLS = ["Task", "Agent", "WebFetch", "WebSearch"]

QUICK_TASKS = ["qa-color-detect", "qa-live-refresh", "qa-wrap", "edit-filesize-boundary", "edit-markup-escape",
               "edit-ellipsis-width"]


def find_claude(explicit: str | None) -> str:
    if explicit:
        return explicit
    found = shutil.which("claude")
    if found:
        return found
    ext = Path.home() / ".vscode" / "extensions"
    cands = sorted(ext.glob("anthropic.claude-code-*/resources/native-binary/claude"))
    if cands:
        return str(cands[-1])
    sys.exit("claude CLI not found; pass --claude-bin /path/to/claude")


def claude_cmd(args, arm: str, prompt: str) -> list[str]:
    cfg = ARMS[arm]
    cmd = [args.claude_bin, "-p", prompt, "--output-format", "stream-json", "--verbose",
           "--model", args.model, "--max-turns", str(args.max_turns),
           "--permission-mode", "acceptEdits",
           "--allowedTools", ",".join(ALLOWED_TOOLS),
           "--disallowedTools", ",".join(DISALLOWED_TOOLS + cfg.get("disallow", []))]
    if args.max_budget_usd:
        cmd += ["--max-budget-usd", str(args.max_budget_usd)]
    if args.mode == "bare":
        cmd.append("--bare")
    if args.mode == "isolated":
        # A fresh CLAUDE_CONFIG_DIR ignores ANTHROPIC_API_KEY from the environment
        # ("Not logged in"); an apiKeyHelper is honoured. It reads the env var, so
        # the key never appears on the command line.
        cmd += ["--settings", json.dumps({"apiKeyHelper": 'printf %s "$ANTHROPIC_API_KEY"'})]
    if args.permission_prompts_none:
        cmd += ["--permission-prompts", "none"]
    if cfg["plugin"]:
        cmd += ["--plugin-dir", str(PLUGIN_DIR)]
    return cmd


def claude_env(args, arm: str, venv: Path, hook_log: Path) -> dict:
    env = bench_env(venv, ARMS[arm].get("env"))
    # Drop session variables inherited when the bench is launched from inside a
    # Claude Code session (CLAUDECODE, CLAUDE_CODE_SESSION_ID, ...).
    for k in [k for k in env if k.startswith("CLAUDE")]:
        del env[k]
    env.update(ARMS[arm].get("env") or {})
    env["THRIFT_HOOK_LOG"] = str(hook_log)
    if args.mode == "isolated":
        cfg_dir = CACHE_DIR / "claude-config"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        env["CLAUDE_CONFIG_DIR"] = str(cfg_dir)
    return env


def index_workdir(workdir: Path) -> float:
    from thriftctx.indexer import index_project

    t = time.monotonic()
    stats = index_project(workdir, full=True)
    if stats.busy or not stats.complete:
        raise RuntimeError(f"indexing failed: {stats.summary()}")
    return time.monotonic() - t


def changed_files(workdir: Path) -> list[str]:
    p = subprocess.run(["git", "-C", str(workdir), "status", "--porcelain"], capture_output=True, text=True)
    return [ln[3:] for ln in p.stdout.splitlines() if ln[3:] and not ln[3:].startswith(".thrift")]


def one_run(args, suite: Suite, task: Task, arm: str, rep: int, base: Path, venv: Path, out_dir: Path) -> dict:
    key = f"{task.id}__{arm}__{rep}"
    run_dir = out_dir / "runs" / key
    run_dir.mkdir(parents=True, exist_ok=True)
    workdir = Path(args.work_root) / key
    make_workdir(base, workdir, task)
    index_s = index_workdir(workdir) if ARMS[arm]["plugin"] else None

    prompt = task.full_prompt(suite.defaults)
    hook_log = run_dir / "hook.jsonl"
    cmd = claude_cmd(args, arm, prompt)
    t0 = time.monotonic()
    timed_out = False
    with open(run_dir / "stream.jsonl", "w") as out, open(run_dir / "stderr.txt", "w") as err:
        try:
            subprocess.run(cmd, cwd=workdir, env=claude_env(args, arm, venv, hook_log), stdout=out, stderr=err,
                           stdin=subprocess.DEVNULL, timeout=args.timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
    wall = time.monotonic() - t0

    m = parse_file(run_dir / "stream.jsonl")
    if task.kind == "edit":
        success, check_tail = run_check(task, workdir, venv)
    else:
        success, check_tail = judge_answer(task, m["answer"]), ""
    modified = changed_files(workdir)
    hook_entries = [json.loads(x) for x in hook_log.read_text().splitlines()] if hook_log.exists() else []
    hook_contexts = [e.get("context") or "" for e in hook_entries if e.get("fired")]
    answered = not timed_out and not m["is_error"] and bool((m["answer"] or "").strip())

    rec = {
        "key": key, "task": task.id, "kind": task.kind, "split": task.split, "arm": arm, "rep": rep,
        "model": args.model, "mode": args.mode,
        # correct = Q&A regexes matched / edit-task check passed (and the run didn't error out)
        "success": bool(success) and answered, "correct": bool(success) and answered, "answered": answered,
        "gold_files": task.gold_files,
        "hook_context": hook_contexts[0] if hook_contexts else None,
        # did the hook's pointer line name a file the task actually needs?
        "hook_gold_hit": any(g in c for g in task.gold_files for c in hook_contexts) if ARMS[arm]["plugin"] else None,
        "timed_out": timed_out, "wall_s": round(wall, 2), "index_s": round(index_s, 2) if index_s else None,
        "check_tail": check_tail, "modified_files": modified,
        "hook_fired": sum(1 for e in hook_entries if e.get("fired")), "hook_calls": len(hook_entries),
        **{k: v for k, v in m.items() if not k.startswith("init_")},
        "plugins_loaded": [p.get("name") for p in m["init_plugins"]] if m["init_plugins"] else [],
        "plugin_errors": m["init_plugin_errors"],
    }
    if not args.keep_workdirs:
        shutil.rmtree(workdir, ignore_errors=True)
    return rec


def validate(suite: Suite, tasks: list[Task]) -> int:
    base = base_checkout(suite.repo)
    venv = test_venv(suite.repo)
    bad = 0
    with tempfile.TemporaryDirectory(prefix="thrift-validate-") as tmp:
        for t in tasks:
            wd = Path(tmp) / t.id
            try:
                make_workdir(base, wd, t)
            except Exception as e:
                print(f"FAIL {t.id}: setup: {e}")
                bad += 1
                continue
            missing = [g for g in t.gold_files if not (wd / g).exists()]
            if missing:
                print(f"FAIL {t.id}: gold files missing: {missing}")
                bad += 1
                continue
            if t.kind != "edit":
                print(f"ok   {t.id} (qa)")
                continue
            broken_ok, _ = run_check(t, wd, venv)
            apply_setup(wd, [{"file": s["file"], "old": s["new"], "new": s["old"]} for s in t.setup])
            fixed_ok, tail = run_check(t, wd, venv)
            if broken_ok or not fixed_ok:
                print(f"FAIL {t.id}: check passes with bug={broken_ok}, passes when fixed={fixed_ok}\n{tail}")
                bad += 1
            else:
                print(f"ok   {t.id} (bug detected by check; fix restores it)")
    print(f"\n{len(tasks) - bad}/{len(tasks)} tasks valid")
    return 1 if bad else 0


def smoke(args, suite: Suite, out_dir: Path) -> int:
    base = base_checkout(suite.repo)
    venv = test_venv(suite.repo)
    probe = Task(id="smoke", kind="qa", prompt="Where is the color system detection implemented? Reply with just "
                 "the file path.", expect_all=["console\\.py"], no_suffix=True)
    problems = 0
    for arm in args.arms:
        rec = one_run(args, suite, probe, arm, 0, base, venv, out_dir)
        tools = parse_file(out_dir / "runs" / rec["key"] / "stream.jsonl")
        names = tools["init_tools"]
        servers = {s.get("name"): s.get("status") for s in tools["init_mcp_servers"]}
        has_tools = any(n.startswith(THRIFT_TOOL_PREFIX) for n in names)
        expect_plugin = ARMS[arm]["plugin"]
        expect_tools = expect_plugin and THRIFT_SERVER_RULE not in ARMS[arm].get("disallow", [])
        expect_hook = expect_plugin and ARMS[arm].get("env", {}).get("THRIFT_HOOK") != "0"
        checks = {
            "plugin loaded": ("thrift" in rec["plugins_loaded"]) == expect_plugin,
            "no plugin errors": not rec["plugin_errors"],
            "mcp tools": has_tools == expect_tools,
            "mcp connected": (servers.get("plugin:thrift:thrift") == "connected") if expect_plugin else True,
            "hook ran": (rec["hook_calls"] > 0) == expect_hook,
            "answered": rec["success"],
        }
        ok = all(checks.values())
        problems += not ok
        print(f"{'ok ' if ok else 'BAD'} {arm:10} " + "  ".join(f"{k}={'y' if v else 'N'}" for k, v in checks.items())
              + f"  | in={rec['input_total']} out={rec['output_tokens']} ${rec['cost_usd'] or 0:.4f}"
              + f" thrift_calls={rec['thrift_calls']} hook_fired={rec['hook_fired']}")
        if not ok:
            print(f"     servers={servers} plugin_errors={rec['plugin_errors']} stderr: "
                  f"{(out_dir / 'runs' / rec['key'] / 'stderr.txt').read_text()[-400:]}")
    return 1 if problems else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tasks", default=str(Path(__file__).parent / "tasks.yaml"))
    ap.add_argument("--arms", default="baseline,thrift", help=f"comma list of {','.join(ARMS)}")
    ap.add_argument("--task", action="append", help="only these task ids (repeatable)")
    ap.add_argument("--split", action="append", help="only these splits: dev, holdout, overhead")
    ap.add_argument("--repeats", type=int, default=5)
    ap.add_argument("--quick", action="store_true", help=f"{len(QUICK_TASKS)} tasks x 2 repeats")
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--max-turns", type=int, default=40)
    ap.add_argument("--max-budget-usd", type=float, default=2.0)
    ap.add_argument("--timeout", type=float, default=900, help="seconds per run")
    ap.add_argument("--mode", choices=["isolated", "bare", "user"], default="isolated",
                    help="isolated: empty CLAUDE_CONFIG_DIR (default); bare: --bare (no Grep/Glob, plugin hooks "
                         "may be skipped); user: your normal config (not isolated!)")
    ap.add_argument("--permission-prompts-none", action="store_true",
                    help="pass --permission-prompts none (Claude Code >= 2.1.259)")
    ap.add_argument("--claude-bin")
    ap.add_argument("--out", help="results dir (default bench/results/<timestamp>); reuse to resume")
    ap.add_argument("--work-root", default=os.path.join(tempfile.gettempdir(), "thrift-bench"))
    ap.add_argument("--seed", type=int, default=1234)
    ap.add_argument("--keep-workdirs", action="store_true")
    ap.add_argument("--validate", action="store_true", help="validate tasks and exit (no API calls)")
    ap.add_argument("--smoke", action="store_true", help="one probe run per arm to verify the setup")
    args = ap.parse_args()

    suite = load_suite(args.tasks)
    args.arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    unknown = [a for a in args.arms if a not in ARMS]
    if unknown:
        sys.exit(f"unknown arms: {unknown}")

    tasks = suite.select(args.task or (QUICK_TASKS if args.quick else None), args.split)
    if args.validate:
        return validate(suite, tasks)

    args.claude_bin = find_claude(args.claude_bin)
    if args.mode in ("isolated", "bare") and not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY is required for --mode isolated/bare: put it in .env at the repo root "
                 "(or export it), or use --mode user (not isolated)")
    if args.mode == "user":
        print("warning: --mode user loads your own settings/plugins/CLAUDE.md into BOTH arms", file=sys.stderr)

    # absolute: the hook log path is used from inside each run's work dir
    out_dir = Path(args.out or REPO_ROOT / "bench" / "results" / datetime.now().strftime("%Y%m%d-%H%M%S")).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    Path(args.work_root).mkdir(parents=True, exist_ok=True)

    if args.smoke:
        return smoke(args, suite, out_dir / "smoke")

    repeats = 2 if args.quick and args.repeats == 5 else args.repeats
    runs_path = out_dir / "runs.jsonl"
    done = set()
    if runs_path.exists():
        done = {json.loads(x)["key"] for x in runs_path.read_text().splitlines() if x.strip()}
    (out_dir / "config.json").write_text(json.dumps({
        "argv": sys.argv, "arms": args.arms, "repeats": repeats, "model": args.model, "mode": args.mode,
        "tasks": [t.id for t in tasks], "repo": suite.repo, "claude_bin": args.claude_bin,
        "claude_version": subprocess.run([args.claude_bin, "--version"], capture_output=True, text=True).stdout.strip(),
        "started": datetime.now().isoformat(timespec="seconds"),
    }, indent=2))

    base = base_checkout(suite.repo)
    venv = test_venv(suite.repo)
    rng = random.Random(args.seed)
    plan = []
    for rep in range(repeats):
        order = tasks[:]
        rng.shuffle(order)
        for t in order:
            arms = args.arms[:]
            rng.shuffle(arms)  # interleave arms per task to spread cache/time effects
            plan.extend((t, a, rep) for a in arms)

    total = len(plan)
    print(f"{total} runs ({len(tasks)} tasks x {len(args.arms)} arms x {repeats} repeats) -> {out_dir}")
    for i, (t, arm, rep) in enumerate(plan, 1):
        key = f"{t.id}__{arm}__{rep}"
        if key in done:
            continue
        try:
            rec = one_run(args, suite, t, arm, rep, base, venv, out_dir)
        except Exception as e:  # keep going; record the failure
            rec = {"key": key, "task": t.id, "kind": t.kind, "split": t.split, "arm": arm, "rep": rep,
                   "success": False, "errors": [f"harness: {type(e).__name__}: {e}"]}
        with open(runs_path, "a") as f:
            f.write(json.dumps(rec) + "\n")
        print(f"[{i}/{total}] {key:45} success={rec.get('success')} in={rec.get('input_total')} "
              f"out={rec.get('output_tokens')} ${rec.get('cost_usd') or 0:.3f} files_read={rec.get('files_read')} "
              f"thrift={rec.get('thrift_calls')} {rec.get('wall_s')}s", flush=True)

    print(f"\nDone. Report: uv run python bench/report.py {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
