#!/usr/bin/env python3
"""A/B benchmark: Claude Code headless runs with and without the thrift plugin.

Usage (from the repo root):
  uv run python bench/run.py --validate                    # check tasks (bugs fail, fixes pass)
  uv run python bench/run.py --smoke                       # verify plugin/MCP/hook load in each arm
  uv run python bench/run.py --tasks edit --repeats 2 --max-cost 2
  uv run python bench/run.py --quick                       # small paired run
  uv run python bench/report.py bench/results/<ts>         # comparison report

Auth (credentials from the environment or the repo's .env; never printed or
put on the command line):
  api-key       ANTHROPIC_API_KEY (passed to Claude Code via an apiKeyHelper)
  subscription  CLAUDE_CODE_OAUTH_TOKEN from `claude setup-token` (Pro/Max/Team/
                Enterprise); costs in the report are estimated API prices
  auto          api-key if ANTHROPIC_API_KEY is set, else subscription (default)

The default "isolated" mode runs with an empty CLAUDE_CONFIG_DIR so none of
your own settings, plugins, hooks, MCP servers or CLAUDE.md leak into either arm.
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
from pricing import estimate_cost

THRIFT_SERVER_RULE = "mcp__plugin_thrift_thrift"

ARMS = {
    "baseline": {"plugin": False},
    "thrift": {"plugin": True},
    "hook-only": {"plugin": True, "disallow": [THRIFT_SERVER_RULE]},
    "mcp-only": {"plugin": True, "env": {"THRIFT_HOOK": "0"}},
    # reprouter arms: every request goes through the local proxy (record mode = no transforms, but
    # exact bodies + usage are captured); "router" applies the enabled transforms with the
    # preserved-thinking canary on, so any accidental history edit fails loudly.
    "base-proxy": {"plugin": False, "proxy": "record"},
    "router": {"plugin": False, "proxy": "on", "canary": True},
    "router-hook": {"plugin": False, "proxy": "record", "hook": True},
}
ROUTER_SRC = REPO_ROOT / "router" / "src"

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


def resolve_auth(requested: str, mode: str, environ: dict) -> tuple[str, list[str], dict]:
    """Pick the auth method and return (auth, extra claude args, env overrides).

    Env overrides map a variable to a value, or to None to remove it from the
    child environment. Secrets are only ever referenced by variable name.
    """
    has_key = bool(environ.get("ANTHROPIC_API_KEY"))
    has_token = bool(environ.get("CLAUDE_CODE_OAUTH_TOKEN"))
    auth = requested
    if auth == "auto":
        auth = "api-key" if has_key else "subscription"
    if auth == "api-key":
        if not has_key:
            raise SystemExit("--auth api-key needs ANTHROPIC_API_KEY (in .env at the repo root, or exported)")
        extra = []
        if mode == "isolated":
            # A fresh CLAUDE_CONFIG_DIR ignores ANTHROPIC_API_KEY from the environment
            # ("Not logged in"); an apiKeyHelper is honoured. It reads the env var, so
            # the key never appears on the command line.
            extra = ["--settings", json.dumps({"apiKeyHelper": 'printf %s "$ANTHROPIC_API_KEY"'})]
        return auth, extra, {"CLAUDE_CODE_OAUTH_TOKEN": None}
    if auth == "subscription":
        if mode == "bare":
            raise SystemExit("--mode bare does not read CLAUDE_CODE_OAUTH_TOKEN; use --mode isolated for "
                             "subscription runs")
        if mode == "isolated" and not has_token:
            raise SystemExit(
                "No ANTHROPIC_API_KEY and no CLAUDE_CODE_OAUTH_TOKEN.\n"
                "For subscription runs: run `claude setup-token` in your terminal (Pro/Max/Team/Enterprise "
                "plan), then add CLAUDE_CODE_OAUTH_TOKEN=<token> to .env at the repo root.")
        # an API key in the environment would outrank the OAuth token
        overrides: dict = {"ANTHROPIC_API_KEY": None}
        if has_token:
            overrides["CLAUDE_CODE_OAUTH_TOKEN"] = environ["CLAUDE_CODE_OAUTH_TOKEN"]
        return auth, [], overrides
    raise SystemExit(f"unknown --auth {requested}")


def select_tasks(suite: Suite, spec: str | None, ids: list[str] | None, splits: list[str] | None,
                 quick: bool) -> list[Task]:
    """--tasks takes kinds and/or task ids, comma-separated: "edit", "qa", "all",
    "edit,qa-wrap". --task (repeatable ids) and --quick still work."""
    tasks = suite.select(ids or (QUICK_TASKS if quick else None), splits)
    if spec and spec != "all":
        wanted = {x.strip() for x in spec.split(",") if x.strip()}
        known_ids = {t.id for t in suite.tasks}
        unknown = wanted - {"edit", "qa"} - known_ids
        if unknown:
            raise SystemExit(f"--tasks: unknown kinds/ids {sorted(unknown)} (kinds: edit, qa, all)")
        tasks = [t for t in tasks if t.kind in wanted or t.id in wanted]
    return tasks


def run_cost(rec: dict) -> float:
    """Cost used for --max-cost and the report: the API bill estimate Claude
    Code reports for API-key runs, our price-table estimate for subscription runs."""
    if rec.get("cost_basis") == "estimated":
        return rec.get("est_cost_usd") or 0.0
    return rec.get("cost_usd") or rec.get("est_cost_usd") or 0.0


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
    cmd += args.auth_args
    if args.permission_prompts_none:
        cmd += ["--permission-prompts", "none"]
    if cfg["plugin"]:
        cmd += ["--plugin-dir", str(PLUGIN_DIR)]
    return cmd


def proxy_cmd(port: int, mode: str, run_dir: Path, canary: bool) -> list[str]:
    cmd = [sys.executable, "-m", "reprouter.cli", "proxy", "--port", str(port), "--mode", mode,
           "--corpus-dir", str(run_dir / "proxy"), "--state-file", str(run_dir / "proxy-state.json")]
    return cmd + (["--canary"] if canary else [])


def hook_settings(python: str) -> dict:
    return {"hooks": {"PostToolUse": [{"matcher": "Bash", "hooks": [
        {"type": "command", "command": f'"{python}" -m reprouter.hook', "timeout": 30}]}]}}


def merge_settings(cmd: list[str], extra: dict) -> list[str]:
    """Merge ``extra`` into the command's --settings JSON (adding the flag if absent)."""
    cmd = list(cmd)
    if "--settings" in cmd:
        i = cmd.index("--settings") + 1
        cmd[i] = json.dumps({**json.loads(cmd[i]), **extra})
    else:
        cmd += ["--settings", json.dumps(extra)]
    return cmd


def summarize_proxy_corpus(d: Path) -> dict:
    out = {"proxy_requests": 0, "proxy_errors": 0, "router_blocks": 0, "router_saved_est": 0,
           "router_transforms": {}}
    if not Path(d).is_dir():
        return out
    for f in sorted(Path(d).glob("*.jsonl")):
        for line in f.read_text().splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            out["proxy_requests"] += 1
            out["proxy_errors"] += int(r.get("status", 200) >= 400)
            for dec in r.get("decisions") or []:
                out["router_blocks"] += 1
                out["router_saved_est"] += dec.get("before", 0) - dec.get("after", 0)
                out["router_transforms"][dec["transform"]] = out["router_transforms"].get(dec["transform"], 0) + 1
    return out


def _start_proxy(args, arm: str, run_dir: Path):
    import socket

    with socket.socket() as sk:
        sk.bind(("127.0.0.1", 0))
        port = sk.getsockname()[1]
    env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE")}
    env["PYTHONPATH"] = os.pathsep.join([str(ROUTER_SRC), str(PLUGIN_DIR / "src")])
    env["REPROUTER_TRANSFORMS"] = args.router_transforms
    env["REPROUTER_KILL_FILE"] = str(run_dir / "reprouter-off")
    log = open(run_dir / "proxy.log", "w")
    proc = subprocess.Popen(proxy_cmd(port, ARMS[arm]["proxy"], run_dir, ARMS[arm].get("canary", False)),
                            env=env, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return proc, port
        except OSError:
            time.sleep(0.2)
    proc.terminate()
    raise RuntimeError("reprouter proxy did not start (see proxy.log)")


def claude_env(args, arm: str, venv: Path, hook_log: Path) -> dict:
    env = bench_env(venv, ARMS[arm].get("env"))
    # Drop session variables inherited when the bench is launched from inside a
    # Claude Code session (CLAUDECODE, CLAUDE_CODE_SESSION_ID, ...).
    for k in [k for k in env if k.startswith("CLAUDE")]:
        del env[k]
    for k, v in args.auth_env.items():
        if v is None:
            env.pop(k, None)
        else:
            env[k] = v
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
    env = claude_env(args, arm, venv, hook_log)
    proxy = None
    if ARMS[arm].get("hook"):
        cmd = merge_settings(cmd, hook_settings(sys.executable))
        env["PYTHONPATH"] = os.pathsep.join([str(ROUTER_SRC), str(PLUGIN_DIR / "src")])
        env["REPROUTER_TRANSFORMS"] = args.router_transforms
        env["REPROUTER_HOOK_LOG"] = str(run_dir / "reprouter-hook.jsonl")
    if ARMS[arm].get("proxy"):
        proxy, port = _start_proxy(args, arm, run_dir)
        env["ANTHROPIC_BASE_URL"] = f"http://127.0.0.1:{port}"
    t0 = time.monotonic()
    timed_out = False
    try:
        with open(run_dir / "stream.jsonl", "w") as out, open(run_dir / "stderr.txt", "w") as err:
            try:
                subprocess.run(cmd, cwd=workdir, env=env, stdout=out, stderr=err,
                               stdin=subprocess.DEVNULL, timeout=args.timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
    finally:
        if proxy is not None:
            proxy.terminate()
            proxy.wait(timeout=10)
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
        "model": args.model, "mode": args.mode, "auth": args.auth,
        "cost_basis": "estimated" if args.auth == "subscription" else "reported",
        "est_cost_usd": estimate_cost(m["model_usage"], m["cache_write_1h_fraction"]),
        # correct = Q&A regexes matched / edit-task check passed (and the run didn't error out)
        "success": bool(success) and answered, "correct": bool(success) and answered, "answered": answered,
        "gold_files": task.gold_files,
        "hook_context": hook_contexts[0] if hook_contexts else None,
        # did the hook's pointer line name a file the task actually needs?
        "hook_gold_hit": any(g in c for g in task.gold_files for c in hook_contexts) if ARMS[arm]["plugin"] else None,
        "timed_out": timed_out, "wall_s": round(wall, 2), "index_s": round(index_s, 2) if index_s else None,
        "check_tail": check_tail, "modified_files": modified,
        "hook_fired": sum(1 for e in hook_entries if e.get("fired")), "hook_calls": len(hook_entries),
        **{k: v for k, v in m.items() if not k.startswith("init_") and k != "model_usage"},
        **(summarize_proxy_corpus(run_dir / "proxy") if ARMS[arm].get("proxy") else {}),
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
            if t.reference:  # a task with no injected bug: the reference solution must make the check pass
                apply_setup(wd, t.reference)
            else:
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
              + f"  | in={rec['input_total']} out={rec['output_tokens']} ${run_cost(rec):.4f}"
              + f" thrift_calls={rec['thrift_calls']} hook_fired={rec['hook_fired']}")
        if "Not logged in" in (rec.get("answer") or ""):
            print(f"     Claude Code says 'Not logged in' with --auth {args.auth}: check the credential in .env")
        if not ok:
            print(f"     servers={servers} plugin_errors={rec['plugin_errors']} stderr: "
                  f"{(out_dir / 'runs' / rec['key'] / 'stderr.txt').read_text()[-400:]}")
    return 1 if problems else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tasks-file", default=str(Path(__file__).parent / "tasks.yaml"))
    ap.add_argument("--tasks", help="kinds and/or ids, comma-separated: edit, qa, all, or task ids")
    ap.add_argument("--arms", default="baseline,thrift", help=f"comma list of {','.join(ARMS)}")
    ap.add_argument("--task", action="append", help="only these task ids (repeatable)")
    ap.add_argument("--split", action="append", help="only these splits: dev, holdout, overhead")
    ap.add_argument("--repeats", type=int, default=5)
    ap.add_argument("--quick", action="store_true", help=f"{len(QUICK_TASKS)} tasks x 2 repeats")
    ap.add_argument("--model", default="haiku", help="Claude Code model alias or id (default: haiku)")
    ap.add_argument("--router-transforms", default=None,
                    help="comma list for the router arms (default: router/results/defaults.json 'enabled')")
    ap.add_argument("--auth", choices=["auto", "api-key", "subscription"], default="auto")
    ap.add_argument("--max-cost", type=float,
                    help="stop starting new runs once total cost in this results dir reaches this many USD "
                         "(estimated API cost for subscription runs); finished runs are kept")
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

    suite = load_suite(args.tasks_file)
    args.arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    if args.router_transforms is None:
        try:
            args.router_transforms = ",".join(json.loads(
                (REPO_ROOT / "router" / "results" / "defaults.json").read_text())["enabled"])
        except (OSError, ValueError, KeyError):
            args.router_transforms = ""
    unknown = [a for a in args.arms if a not in ARMS]
    if unknown:
        sys.exit(f"unknown arms: {unknown}")

    tasks = select_tasks(suite, args.tasks, args.task, args.split, args.quick)
    if not tasks:
        sys.exit("no tasks selected")
    if args.validate:
        return validate(suite, tasks)

    args.claude_bin = find_claude(args.claude_bin)
    args.auth, args.auth_args, args.auth_env = resolve_auth(args.auth, args.mode, os.environ)
    print(f"auth: {args.auth} | model: {args.model} | mode: {args.mode}")
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
    spent = 0.0
    if runs_path.exists():
        for x in runs_path.read_text().splitlines():
            if x.strip():
                prev = json.loads(x)
                done.add(prev["key"])
                spent += run_cost(prev)
    (out_dir / "config.json").write_text(json.dumps({
        "argv": sys.argv, "arms": args.arms, "repeats": repeats, "model": args.model, "mode": args.mode,
        "auth": args.auth, "max_cost": args.max_cost,
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
        if args.max_cost is not None and spent >= args.max_cost:
            print(f"--max-cost ${args.max_cost:.2f} reached (spent ${spent:.3f}); not starting the remaining "
                  f"runs. Partial results kept in {runs_path}")
            break
        try:
            rec = one_run(args, suite, t, arm, rep, base, venv, out_dir)
        except Exception as e:  # keep going; record the failure
            rec = {"key": key, "task": t.id, "kind": t.kind, "split": t.split, "arm": arm, "rep": rep,
                   "success": False, "errors": [f"harness: {type(e).__name__}: {e}"]}
        with open(runs_path, "a") as f:
            f.write(json.dumps(rec) + "\n")
        spent += run_cost(rec)
        print(f"[{i}/{total}] {key:45} correct={rec.get('success')} in={rec.get('input_total')} "
              f"out={rec.get('output_tokens')} ${run_cost(rec):.3f} (total ${spent:.2f}) "
              f"files_read={rec.get('files_read')} "
              f"thrift={rec.get('thrift_calls')} {rec.get('wall_s')}s", flush=True)

    print(f"\nDone. Report: uv run python bench/report.py {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
