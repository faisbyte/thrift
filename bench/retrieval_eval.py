#!/usr/bin/env python3
"""Offline retrieval quality: does the index surface each task's gold files?

No LLM calls; runs in seconds. Use it to tune ranking weights on the `dev`
split, then confirm on `holdout`.

  uv run python bench/retrieval_eval.py
  uv run python bench/retrieval_eval.py --split dev --set test_penalty=0.5 --set exact_symbol=0.8
"""

from __future__ import annotations

import argparse
import dataclasses
import sys
import tempfile
from pathlib import Path

from common import base_checkout, load_suite, make_workdir

from thriftctx import store
from thriftctx.hook_prompt import build_context
from thriftctx.indexer import index_project
from thriftctx.rank import BM25Ranker, Weights


def file_ranking(hits) -> list[str]:
    out: list[str] = []
    for h in hits:
        if h.path not in out:
            out.append(h.path)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tasks-file", default=str(Path(__file__).parent / "tasks.yaml"))
    ap.add_argument("--split", action="append", help="dev / holdout (default: both)")
    ap.add_argument("-k", type=int, default=10)
    ap.add_argument("--set", action="append", default=[], metavar="FIELD=VALUE", help="override a ranking weight")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    overrides = {}
    for kv in args.set:
        name, _, val = kv.partition("=")
        if name not in {f.name for f in dataclasses.fields(Weights)}:
            sys.exit(f"unknown weight {name}")
        overrides[name] = type(getattr(Weights(), name))(val)
    weights = dataclasses.replace(Weights(), **overrides)

    suite = load_suite(args.tasks_file)
    tasks = [t for t in suite.select(splits=args.split or ["dev", "holdout"]) if t.gold_files]
    base = base_checkout(suite.repo)

    rows = []
    with tempfile.TemporaryDirectory(prefix="thrift-reval-") as tmp:
        for t in tasks:
            wd = make_workdir(base, Path(tmp) / t.id, t)
            index_project(wd)
            conn = store.open_readonly(wd)
            try:
                hits = BM25Ranker(conn, weights).rank(t.prompt, args.k)
            finally:
                conn.close()
            files = file_ranking(hits)
            gold = set(t.gold_files)
            first = next((i for i, f in enumerate(files, 1) if f in gold), None)
            hook = build_context({"prompt": t.prompt, "cwd": str(wd)}, deadline=5.0)
            rows.append({
                "task": t.id, "split": t.split, "rank": first,
                "hook_fired": bool(hook), "hook_has_gold": any(g in hook for g in gold),
                "top": files[:3],
            })
            if args.verbose:
                print(f"\n## {t.id}  gold={sorted(gold)}  first_gold_rank={first}")
                for h in hits[:5]:
                    print(f"  {h.score:5.2f} cov={h.coverage:.2f} {h.path}:L{h.start_line}-{h.end_line} {h.label}")
                print(f"  hook: {hook or '(silent)'}")

    def recall(k):
        return sum(1 for r in rows if r["rank"] and r["rank"] <= k) / len(rows)

    mrr = sum(1 / r["rank"] for r in rows if r["rank"]) / len(rows)
    print(f"\n{'task':28} {'split':8} {'gold@':>5}  hook   top files")
    for r in rows:
        hook = "gold" if r["hook_has_gold"] else ("fired" if r["hook_fired"] else "-")
        print(f"{r['task']:28} {r['split']:8} {r['rank'] or '-':>5}  {hook:5}  {', '.join(r['top'])}")
    print(f"\n{len(rows)} tasks | recall@1 {recall(1):.2f}  @3 {recall(3):.2f}  @5 {recall(5):.2f}  "
          f"@{args.k} {recall(args.k):.2f} | MRR {mrr:.2f} | hook fired {sum(r['hook_fired'] for r in rows)}"
          f"/{len(rows)}, with gold file {sum(r['hook_has_gold'] for r in rows)}/{len(rows)}")
    if overrides:
        print(f"weights: {overrides}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
