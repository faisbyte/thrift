#!/usr/bin/env python3
"""Comparison report for a benchmark results directory.

  uv run python bench/report.py bench/results/<timestamp>

Writes summary.md and runs.csv next to runs.jsonl and prints the markdown.

Sections:
  1. Per-task tables (edit / Q&A), one row per task x arm: success (= answer
     CORRECT: Q&A regexes matched or edit-task tests passed), mean cost,
     mean tokens (uncached input, cache write, cache read, output), wall time,
     turns, files read. Each task is tagged hook_hit / hook_miss: did the
     prompt hook's pointer line name a gold file (in the plugin arms)?
  2. hook_miss tasks on their own, to see whether a wrong/missing pointer made
     things worse than baseline.
  3. Per-arm totals for edit / Q&A / overall.
  4. Every run with answered vs CORRECT.
  5. Summary: cost change and success change vs baseline per group.
"""

from __future__ import annotations

import csv
import json
import math
import random
import statistics
import sys
from collections import defaultdict
from pathlib import Path

OVERHEAD_SPLIT = "overhead"
BASE = "baseline"
GROUPS = [("edit", "Edit tasks"), ("qa", "Q&A tasks")]

# per task x arm columns: key, header, format
TASK_COLS = [
    ("cost", "mean cost $", "{:.4f}"),
    ("input_tokens", "in (uncached)", "{:,.0f}"),
    ("cache_write_tokens", "cache write", "{:,.0f}"),
    ("cache_read_tokens", "cache read", "{:,.0f}"),
    ("output_tokens", "out", "{:,.0f}"),
    ("wall_s", "wall s", "{:.0f}"),
    ("num_turns", "turns", "{:.1f}"),
    ("files_read", "files read", "{:.1f}"),
]


# --------------------------------------------------------------------------- data


def load(path: Path) -> list[dict]:
    recs: dict[str, dict] = {}
    for line in path.read_text().splitlines():
        if line.strip():
            r = json.loads(line)
            recs[r["key"]] = r  # last write wins (re-runs)
    return list(recs.values())


def cost_of(r: dict, cost_field: str = "cost_usd") -> float | None:
    v = r.get(cost_field)
    return v if v is not None else r.get("cost_usd")


def correct(r: dict) -> bool:
    return bool(r.get("correct", r.get("success")))


def answered(r: dict) -> bool:
    if "answered" in r:
        return bool(r["answered"])
    return not r.get("is_error") and not r.get("timed_out") and bool((r.get("answer") or "").strip())


def mean(xs):
    xs = [x for x in xs if x is not None]
    return statistics.fmean(xs) if xs else None


def fmt(v, f="{:.2f}"):
    return "–" if v is None else f.format(v)


def task_hook_tags(runs: list[dict]) -> dict[str, str]:
    """hook_hit if (most) plugin-arm runs got a pointer naming a gold file,
    hook_miss if the hook named no gold file (or stayed silent), n/a if no
    plugin-arm run recorded it."""
    votes: dict[str, list[bool]] = defaultdict(list)
    for r in runs:
        if r.get("hook_gold_hit") is not None:
            votes[r["task"]].append(bool(r["hook_gold_hit"]))
    tags = {}
    for task in {r["task"] for r in runs}:
        v = votes.get(task)
        tags[task] = "n/a" if not v else ("hook_hit" if sum(v) * 2 >= len(v) else "hook_miss")
    return tags


def agg(rs: list[dict], cost_field: str) -> dict:
    out = {"n": len(rs), "success": mean([1.0 if correct(r) else 0.0 for r in rs]),
           "answered": mean([1.0 if answered(r) else 0.0 for r in rs]),
           "cost": mean([cost_of(r, cost_field) for r in rs])}
    for k, _, _ in TASK_COLS:
        if k != "cost":
            out[k] = mean([r.get(k) for r in rs])
    out["input_total"] = mean([r.get("input_total") for r in rs])
    return out


def pct_change(a, b):
    if a is None or b in (None, 0):
        return None
    return (a / b - 1) * 100


def geo_mean(xs):
    xs = [x for x in xs if x and x > 0]
    return math.exp(statistics.fmean(math.log(x) for x in xs)) if xs else None


def bootstrap_ci(xs, n=2000, seed=0):
    xs = [x for x in xs if x and x > 0]
    if len(xs) < 2:
        return None, None
    rng = random.Random(seed)
    stats = sorted(geo_mean([rng.choice(xs) for _ in xs]) for _ in range(n))
    return stats[int(0.025 * n)], stats[int(0.975 * n) - 1]


def compare(runs: list[dict], arm: str, cost_field: str) -> dict:
    """arm vs baseline over the same tasks (only tasks both arms ran)."""
    by_task = defaultdict(lambda: defaultdict(list))
    for r in runs:
        by_task[r["task"]][r["arm"]].append(r)
    tasks = [t for t, a in by_task.items() if arm in a and BASE in a]
    a_runs = [r for t in tasks for r in by_task[t][arm]]
    b_runs = [r for t in tasks for r in by_task[t][BASE]]
    A, B = agg(a_runs, cost_field), agg(b_runs, cost_field)
    ratios = []
    for t in tasks:
        ca = mean([cost_of(r, cost_field) for r in by_task[t][arm]])
        cb = mean([cost_of(r, cost_field) for r in by_task[t][BASE]])
        if ca and cb:
            ratios.append(ca / cb)
    g = geo_mean(ratios)
    lo, hi = bootstrap_ci(ratios)
    return {
        "tasks": len(tasks), "arm": A, "base": B,
        "cost_change_pct": pct_change(A["cost"], B["cost"]),
        "input_change_pct": pct_change(A["input_total"], B["input_total"]),
        "success_change_pp": None if A["success"] is None or B["success"] is None
        else (A["success"] - B["success"]) * 100,
        "paired_cost_ratio": g, "paired_ci": (lo, hi),
    }


# --------------------------------------------------------------------------- rendering


def _pct(v):
    return "–" if v is None else f"{v:+.0f}%"


def _pp(v):
    return "–" if v is None else f"{v:+.0f} pp"


def _rate(v):
    return "–" if v is None else f"{v * 100:.0f}%"


def task_table(runs: list[dict], arms: list[str], tags: dict[str, str], cost_field: str) -> list[str]:
    head = ["task", "hook", "arm", "runs", "success"] + [h for _, h, _ in TASK_COLS]
    out = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    by_task = defaultdict(lambda: defaultdict(list))
    for r in runs:
        by_task[r["task"]][r["arm"]].append(r)
    for task in sorted(by_task):
        first = True
        for arm in arms:
            rs = by_task[task].get(arm)
            if not rs:
                continue
            a = agg(rs, cost_field)
            row = [task if first else "", tags.get(task, "n/a") if first else "", arm, str(a["n"]),
                   _rate(a["success"])]
            row += [fmt(a[k], f) for k, _, f in TASK_COLS]
            out.append("| " + " | ".join(row) + " |")
            first = False
    return out


def arm_totals(runs: list[dict], arms: list[str], cost_field: str) -> list[str]:
    head = ["group", "arm", "runs", "answered", "success", "mean cost $", "mean in total", "mean out",
            "wall s", "turns", "files read"]
    out = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    for gkey, glabel in GROUPS + [(None, "Overall")]:
        rs_g = [r for r in runs if gkey is None or r.get("kind") == gkey]
        for arm in arms:
            rs = [r for r in rs_g if r["arm"] == arm]
            if not rs:
                continue
            a = agg(rs, cost_field)
            out.append("| " + " | ".join([
                glabel, arm, str(a["n"]), _rate(a["answered"]), _rate(a["success"]), fmt(a["cost"], "{:.4f}"),
                fmt(a["input_total"], "{:,.0f}"), fmt(a["output_tokens"], "{:,.0f}"), fmt(a["wall_s"], "{:.0f}"),
                fmt(a["num_turns"], "{:.1f}"), fmt(a["files_read"], "{:.1f}")]) + " |")
    return out


def run_table(runs: list[dict], tags: dict[str, str], cost_field: str) -> list[str]:
    head = ["run", "answered", "CORRECT", "cost $", "in total", "out", "turns", "files read", "hook pointer"]
    out = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    for r in sorted(runs, key=lambda r: (r.get("kind", ""), r["task"], r["arm"], r.get("rep", 0))):
        hp = r.get("hook_gold_hit")
        hook = "–" if hp is None else ("gold" if hp else ("wrong" if r.get("hook_context") else "silent"))
        out.append("| " + " | ".join([
            r["key"], "yes" if answered(r) else "NO", "✓" if correct(r) else "✗",
            fmt(cost_of(r, cost_field), "{:.4f}"), fmt(r.get("input_total"), "{:,.0f}"),
            fmt(r.get("output_tokens"), "{:,.0f}"), fmt(r.get("num_turns"), "{}"), fmt(r.get("files_read"), "{}"),
            hook]) + " |")
    return out


def summary_lines(runs: list[dict], arms: list[str], tags: dict[str, str], cost_field: str) -> list[str]:
    out = []
    for arm in arms:
        if arm == BASE:
            continue
        out += [f"**{arm} vs baseline** (cost = mean {('estimated API ' if cost_field != 'cost_usd' else '')}"
                f"cost per run; success = answer correct)", "",
                "| group | tasks | runs (arm/base) | cost change | input-token change | success arm / base | "
                "success change | paired cost ratio (95% CI) |",
                "|---|---|---|---|---|---|---|---|"]
        groups = GROUPS + [(None, "Overall")]
        miss = {t for t, v in tags.items() if v == "hook_miss"}
        for gkey, glabel in groups + [("__miss__", "hook_miss tasks only")]:
            if gkey == "__miss__":
                rs = [r for r in runs if r["task"] in miss]
            else:
                rs = [r for r in runs if gkey is None or r.get("kind") == gkey]
            c = compare(rs, arm, cost_field)
            if not c["tasks"]:
                continue
            lo, hi = c["paired_ci"]
            ratio = "–" if c["paired_cost_ratio"] is None else (
                f"{c['paired_cost_ratio']:.2f}" + (f" ({lo:.2f}–{hi:.2f})" if lo is not None else ""))
            out.append("| " + " | ".join([
                glabel, str(c["tasks"]), f"{c['arm']['n']}/{c['base']['n']}", _pct(c["cost_change_pct"]),
                _pct(c["input_change_pct"]), f"{_rate(c['arm']['success'])} / {_rate(c['base']['success'])}",
                _pp(c["success_change_pp"]), ratio]) + " |")
        out.append("")
    return out


def render(runs: list[dict], cfg: dict) -> str:
    arms = cfg.get("arms") or sorted({r["arm"] for r in runs}, key=lambda a: (a != BASE, a))
    main = [r for r in runs if r.get("split") != OVERHEAD_SPLIT]
    overhead = [r for r in runs if r.get("split") == OVERHEAD_SPLIT]
    cost_field = "est_cost_usd" if any(r.get("cost_basis") == "estimated" for r in main) else "cost_usd"
    tags = task_hook_tags(main)
    repo = cfg.get("repo", {})
    out = [f"# thrift benchmark: {repo.get('name', '?')} @ {str(repo.get('sha', '?'))[:10]}", "",
           f"model `{cfg.get('model')}`, mode `{cfg.get('mode')}`, auth `{cfg.get('auth', '?')}`, "
           f"{cfg.get('claude_version', '')}, {len(main)} task runs, repeats {cfg.get('repeats')}", ""]
    if cost_field != "cost_usd":
        out += ["Costs are **estimated API prices** from token counts (runs used a subscription).", ""]
    out += ["success = the answer was CORRECT (Q&A regexes matched / edit-task tests passed). "
            "hook_hit = the prompt hook's pointer line named a file the task needs.", ""]

    for gkey, glabel in GROUPS:
        rs = [r for r in main if r.get("kind") == gkey]
        if rs:
            out += [f"## {glabel}: per task (means over repeats)", ""] + task_table(rs, arms, tags, cost_field) + [""]

    miss = [r for r in main if tags.get(r["task"]) == "hook_miss"]
    out += ["## hook_miss tasks (hook pointer did not name a correct file)", ""]
    if miss:
        out += task_table(miss, arms, tags, cost_field) + [""]
    else:
        out += ["None: on every task the hook pointed at a correct file (or no plugin-arm data)." if
                any(v == "hook_hit" for v in tags.values()) else "No hook data recorded.", ""]

    out += ["## Totals per arm", ""] + arm_totals(main, arms, cost_field) + [""]
    if overhead:
        out += ["## Fixed overhead (trivial prompt)", ""] + arm_totals(overhead, arms, cost_field) + [""]
    out += ["## All runs", ""] + run_table(main, tags, cost_field) + [""]
    errs = [r for r in runs if r.get("errors")]
    if errs:
        out += [f"{len(errs)} runs reported errors (see runs.jsonl `errors`).", ""]
    out += ["## Summary", ""] + summary_lines(main, arms, tags, cost_field)
    return "\n".join(out).rstrip() + "\n"


def write_csv(runs: list[dict], path: Path) -> None:
    cols = ["key", "task", "kind", "split", "arm", "rep", "answered", "correct", "cost_usd", "est_cost_usd",
            "input_total", "input_tokens", "cache_write_tokens", "cache_read_tokens", "output_tokens", "wall_s",
            "num_turns", "files_read", "reads", "reads_ranged", "greps", "globs", "bash_calls", "edits",
            "thrift_calls", "hook_fired", "hook_gold_hit", "tool_result_chars_total", "subtype", "timed_out",
            "index_s"]
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in sorted(runs, key=lambda r: r["key"]):
            w.writerow({**r, "correct": correct(r), "answered": answered(r)})


def main() -> int:
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    d = Path(sys.argv[1])
    runs = load(d / "runs.jsonl")
    cfg = json.loads((d / "config.json").read_text()) if (d / "config.json").exists() else {}
    md = render(runs, cfg)
    (d / "summary.md").write_text(md)
    write_csv(runs, d / "runs.csv")
    print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
