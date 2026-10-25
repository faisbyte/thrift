#!/usr/bin/env python3
"""Comparison tables for a benchmark results directory.

  uv run python bench/report.py bench/results/<timestamp>

Writes summary.md and runs.csv next to runs.jsonl and prints the markdown.
Headline numbers are per-task *paired* ratios (arm / baseline, using each
task's median over repeats) summarised by their geometric mean with a
bootstrap 95% CI over tasks, plus success rate and cost per success.
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

METRICS = [
    # key, label, format
    ("cost_usd", "cost $", "{:.4f}"),
    ("input_total", "input tok (incl. cache)", "{:,.0f}"),
    ("output_tokens", "output tok", "{:,.0f}"),
    ("wall_s", "wall s", "{:.1f}"),
    ("num_turns", "turns", "{:.1f}"),
    ("files_read", "files read", "{:.1f}"),
    ("tool_result_chars_total", "tool output chars", "{:,.0f}"),
]
OVERHEAD_SPLIT = "overhead"


def load(path: Path) -> list[dict]:
    recs: dict[str, dict] = {}
    for line in path.read_text().splitlines():
        if line.strip():
            r = json.loads(line)
            recs[r["key"]] = r  # last write wins (re-runs)
    return list(recs.values())


def med(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def mean(xs):
    xs = [x for x in xs if x is not None]
    return statistics.fmean(xs) if xs else None


def fmt(v, f="{:.2f}"):
    return "–" if v is None else f.format(v)


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


def arm_summary(runs: list[dict], arm: str) -> dict:
    rs = [r for r in runs if r["arm"] == arm]
    ok = [r for r in rs if r.get("success")]
    total_cost = sum(r.get("cost_usd") or 0 for r in rs)
    reads = sum(r.get("reads") or 0 for r in rs)
    ranged = sum(r.get("reads_ranged") or 0 for r in rs)
    out = {
        "arm": arm, "runs": len(rs), "success": len(ok) / len(rs) if rs else None,
        "cost_per_success": total_cost / len(ok) if ok else None, "total_cost": total_cost,
        "ranged_read_pct": 100 * ranged / reads if reads else None,
        "thrift_calls": mean([r.get("thrift_calls") for r in rs]),
        "hook_fired_pct": 100 * mean([1 if (r.get("hook_fired") or 0) > 0 else 0 for r in rs]) if rs else None,
        "limit_hits": sum(1 for r in rs if r.get("subtype") not in (None, "success") or r.get("timed_out")),
    }
    for k, _, _ in METRICS:
        out[f"med_{k}"] = med([r.get(k) for r in rs])
    return out


def paired(runs: list[dict], arm: str, base: str = "baseline") -> dict:
    by = defaultdict(lambda: defaultdict(list))
    for r in runs:
        by[r["task"]][r["arm"]].append(r)
    ratios = {k: [] for k, _, _ in METRICS}
    per_task = []
    for task, arms in sorted(by.items()):
        if arm not in arms or base not in arms:
            continue
        row = {"task": task, "kind": arms[arm][0].get("kind")}
        for k, _, _ in METRICS:
            a, b = med([r.get(k) for r in arms[arm]]), med([r.get(k) for r in arms[base]])
            row[k] = (a, b)
            if a is not None and b:
                ratios[k].append(a / b)
        row["succ"] = (mean([1 if r.get("success") else 0 for r in arms[arm]]),
                       mean([1 if r.get("success") else 0 for r in arms[base]]))
        per_task.append(row)
    summary = {}
    for k, _, _ in METRICS:
        g = geo_mean(ratios[k])
        lo, hi = bootstrap_ci(ratios[k])
        summary[k] = (g, lo, hi, len(ratios[k]))
    return {"summary": summary, "per_task": per_task}


def pct(ratio):
    return "–" if ratio is None else f"{(ratio - 1) * 100:+.0f}%"


def render(runs: list[dict], cfg: dict) -> str:
    arms = cfg.get("arms") or sorted({r["arm"] for r in runs})
    main = [r for r in runs if r.get("split") != OVERHEAD_SPLIT]
    overhead = [r for r in runs if r.get("split") == OVERHEAD_SPLIT]
    out = [f"# thrift benchmark: {cfg.get('repo', {}).get('name', '?')} @ {cfg.get('repo', {}).get('sha', '?')[:10]}",
           "",
           f"model `{cfg.get('model')}`, mode `{cfg.get('mode')}`, {cfg.get('claude_version', '')}, "
           f"{len(main)} task runs + {len(overhead)} overhead runs, repeats {cfg.get('repeats')}",
           "", "## Per arm (medians over all task runs)", ""]
    head = ["arm", "runs", "success", "cost/success $"] + [lbl for _, lbl, _ in METRICS] + \
           ["ranged reads", "thrift calls/run", "hook fired", "limit hits"]
    out.append("| " + " | ".join(head) + " |")
    out.append("|" + "---|" * len(head))
    for arm in arms:
        s = arm_summary(main, arm)
        if not s["runs"]:
            continue
        row = [arm, str(s["runs"]), fmt(s["success"] and 100 * s["success"], "{:.0f}%"),
               fmt(s["cost_per_success"], "{:.4f}")]
        row += [fmt(s[f"med_{k}"], f) for k, _, f in METRICS]
        row += [fmt(s["ranged_read_pct"], "{:.0f}%"), fmt(s["thrift_calls"], "{:.1f}"),
                fmt(s["hook_fired_pct"], "{:.0f}%"), str(s["limit_hits"])]
        out.append("| " + " | ".join(row) + " |")

    for arm in arms:
        if arm == "baseline" or not any(r["arm"] == arm for r in main):
            continue
        p = paired(main, arm)
        out += ["", f"## {arm} vs baseline (paired per task; geometric mean of ratios, 95% bootstrap CI)", ""]
        out.append("| metric | change | 95% CI | tasks |")
        out.append("|---|---|---|---|")
        for k, lbl, _ in METRICS:
            g, lo, hi, n = p["summary"][k]
            ci = f"{pct(lo)} … {pct(hi)}" if lo is not None else "–"
            out.append(f"| {lbl} | {pct(g)} | {ci} | {n} |")
        out += ["", f"### Per task: {arm} / baseline (medians)", ""]
        out.append("| task | kind | success (arm/base) | cost $ (arm/base) | input tok (arm/base) | files read (arm/base) |")
        out.append("|---|---|---|---|---|---|")
        for row in p["per_task"]:
            sa, sb = row["succ"]
            ca, cb = row["cost_usd"]
            ia, ib = row["input_total"]
            fa, fb = row["files_read"]
            out.append(f"| {row['task']} | {row['kind']} | {fmt(sa and 100 * sa, '{:.0f}')}% / "
                       f"{fmt(sb and 100 * sb, '{:.0f}')}% | {fmt(ca, '{:.3f}')} / {fmt(cb, '{:.3f}')} | "
                       f"{fmt(ia, '{:,.0f}')} / {fmt(ib, '{:,.0f}')} | {fmt(fa, '{:.0f}')} / {fmt(fb, '{:.0f}')} |")

    if overhead:
        out += ["", "## Fixed overhead (trivial prompt; tool schemas + hook)", "",
                "| arm | input tok (median) | cost $ (median) |", "|---|---|---|"]
        for arm in arms:
            rs = [r for r in overhead if r["arm"] == arm]
            if rs:
                out.append(f"| {arm} | {fmt(med([r.get('input_total') for r in rs]), '{:,.0f}')} | "
                           f"{fmt(med([r.get('cost_usd') for r in rs]), '{:.4f}')} |")

    errs = [r for r in runs if r.get("errors")]
    if errs:
        out += ["", f"{len(errs)} runs reported errors (see runs.jsonl `errors`)."]
    return "\n".join(out) + "\n"


def write_csv(runs: list[dict], path: Path) -> None:
    cols = ["key", "task", "kind", "split", "arm", "rep", "success", "cost_usd", "input_total", "input_tokens",
            "cache_write_tokens", "cache_read_tokens", "output_tokens", "wall_s", "num_turns", "files_read", "reads",
            "reads_ranged", "greps", "globs", "bash_calls", "edits", "thrift_calls", "hook_fired",
            "tool_result_chars_total", "subtype", "timed_out", "index_s"]
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in sorted(runs, key=lambda r: r["key"]):
            w.writerow(r)


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
