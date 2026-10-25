"""Exact per-transform savings and history checks for a bench smoke run ($0: count_tokens only).

    uv run python router/scripts/smoke_report.py bench/results/smoke-heavy

For every proxied run, each recorded *original* main request is re-routed offline with that arm's
transform set and the session's pinned min_msg_index (routing is deterministic, so this
reproduces what the proxy sent). Then:
  - exact request tokens, original vs routed (count_tokens), per request
  - per-decision exact block savings, attributed to the transform, priced cache-aware: the block is
    written once (5m TTL, 1.25x input) on entry and read (cache-read price) on every later request
  - offline history check: each routed request's messages must be a byte prefix of the next
    routed request's (ignoring cache_control, which Claude Code moves), wherever the originals are
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO / "router" / "src"), str(REPO / "plugins" / "thrift" / "src"), str(REPO / "bench")]

from pricing import CACHE_WRITE_5M, price_for  # noqa: E402
from reprouter.config import build_policy  # noqa: E402
from reprouter.proxy.server import session_key  # noqa: E402
from reprouter.request import parse_blocks  # noqa: E402
from reprouter.router import route  # noqa: E402
from reprouter.tokens import AnthropicCountClient, TokenCounter, load_dotenv  # noqa: E402

import run as bench_run  # noqa: E402


def strip_cc(x):
    if isinstance(x, dict):
        return {k: strip_cc(v) for k, v in x.items() if k != "cache_control"}
    if isinstance(x, list):
        return [strip_cc(v) for v in x]
    return x


def countable(body: dict) -> dict:
    """count_tokens rejects per-message extras (e.g. output_config on Claude Code's mid-conversation
    system messages, thinking.display "updates"); keep role+content and drop display. Applied to
    both sides, so differences stay exact."""
    out = {**body, "messages": [{"role": m["role"], "content": m["content"]} for m in body.get("messages", [])]}
    if isinstance(out.get("thinking"), dict):
        out["thinking"] = {k: v for k, v in out["thinking"].items() if k != "display"}
    return out


def norm(msgs: list) -> list:
    """Claude Code sends the newest message's string content as a one-item text list (to hang a
    cache marker on); the API treats both forms alike, so compare in list form."""
    return [{**m, "content": [{"type": "text", "text": m["content"]}]} if isinstance(m.get("content"), str) else m
            for m in strip_cc(msgs)]


def is_prefix(a: list, b: list) -> bool:
    return len(a) <= len(b) and norm(a) == norm(b[:len(a)])


def block_text(req: dict, path) -> str:
    for b in parse_blocks(req):
        if tuple(b.path) == tuple(path):
            return b.text
    return ""


def analyse_run(run_dir: Path, arm: str, counter_for) -> dict:
    rows = []
    for f in sorted((run_dir / "proxy").glob("*.jsonl")):
        rows += [json.loads(x) for x in f.read_text().splitlines() if x.strip()]
    state = json.loads((run_dir / "proxy-state.json").read_text()) if (run_dir / "proxy-state.json").exists() else {}
    main = [r for r in rows if (r.get("request_class") or "main") == "main"]
    out = {"requests": len(rows), "main_requests": len(main),
           "classes": dict(sorted({r.get("request_class") or "?": 0 for r in rows}.items())),
           "statuses": sorted({r["status"] for r in rows}),
           "thinking_param": sorted({json.dumps((r["body"] or {}).get("thinking"), sort_keys=True) for r in main}),
           "assistant_thinking_blocks_in_history": 0,
           "history_check": {"pairs": 0, "orig_prefix_ok": 0, "routed_prefix_ok": 0, "router_broke": 0},
           "tokens_original": 0, "tokens_routed": 0, "by_transform": {}}
    for r in rows:
        out["classes"][r.get("request_class") or "?"] = out["classes"].get(r.get("request_class") or "?", 0) + 1
    transforms = bench_run.ARMS[arm].get("transforms")
    if not main:
        return out
    model = main[-1]["body"].get("model", "claude-haiku-5-5")
    counter = counter_for(model)
    p_in, _, p_read = price_for(model)
    out["assistant_thinking_blocks_in_history"] = sum(
        1 for m in main[-1]["body"].get("messages", []) if m.get("role") == "assistant"
        for c in (m.get("content") if isinstance(m.get("content"), list) else []) if c.get("type") == "thinking")

    policy = build_policy(transforms.split(",")) if transforms else None
    routed_bodies = []
    for r in main:
        body = r["body"]
        if policy is not None and r["status"] < 400:
            min_idx = state.get(session_key(body), 0)
            routed, decs = route(body, policy, min_msg_index=min_idx)
            decs = [d for d in decs if d.transform]
        else:
            routed, decs = body, []
        routed_bodies.append((body, routed, decs))

    # exact request-level tokens over every successful main request
    for body, routed, _ in routed_bodies:
        out["tokens_original"] += counter.count_request(countable(body))
        out["tokens_routed"] += counter.count_request(countable(routed))

    # history check over consecutive main requests of the same session
    for (b0, r0, _), (b1, r1, _) in zip(routed_bodies, routed_bodies[1:]):
        if session_key(b0) != session_key(b1):
            continue
        hc = out["history_check"]
        hc["pairs"] += 1
        o_ok = is_prefix(b0["messages"], b1["messages"])
        r_ok = is_prefix(r0["messages"], r1["messages"])
        hc["orig_prefix_ok"] += o_ok
        hc["routed_prefix_ok"] += r_ok
        hc["router_broke"] += o_ok and not r_ok

    # per-decision savings: entry request = first request whose decisions include the block path
    n = len(routed_bodies)
    seen: set = set()
    for i, (body, routed, decs) in enumerate(routed_bodies):
        for d in decs:
            key = tuple(d.path)
            if key in seen:
                continue
            seen.add(key)
            before = counter.count_text(block_text(body, d.path))
            after = counter.count_text(block_text(routed, d.path))
            saved = before - after
            later = n - i - 1
            t = out["by_transform"].setdefault(d.transform, {"blocks": 0, "tokens_before": 0, "tokens_after": 0,
                                                            "token_requests_saved": 0, "usd_saved": 0.0})
            t["blocks"] += 1
            t["tokens_before"] += before
            t["tokens_after"] += after
            t["token_requests_saved"] += saved * (1 + later)
            t["usd_saved"] += saved * (p_in * CACHE_WRITE_5M + p_read * later) / 1e6
    return out


def main(argv: list[str]) -> int:
    results = Path(argv[1])
    load_dotenv()
    client = AnthropicCountClient()
    counters: dict = {}

    def counter_for(model):
        if model not in counters:
            counters[model] = TokenCounter(client, REPO / "router" / ".cache" / "counts.jsonl", model)
        return counters[model]

    report = {}
    runs = {json.loads(x)["key"]: json.loads(x) for x in (results / "runs.jsonl").read_text().splitlines() if x.strip()}
    for key, rec in sorted(runs.items()):
        run_dir = results / "runs" / key
        a = analyse_run(run_dir, rec["arm"], counter_for)
        a.update({k: rec.get(k) for k in ("arm", "success", "cost_usd", "num_turns", "input_total", "output_tokens",
                                          "cache_write_tokens", "cache_read_tokens", "reads", "reads_after_write",
                                          "edits", "bash_calls", "proxy_errors", "router_blocks",
                                          "router_transforms", "errors", "subtype", "check_tail")})
        a.update(bench_run.summarize_proxy_corpus(run_dir / "proxy"))  # recomputed with the current rules
        report[key] = a
    for t in (r["by_transform"] for r in report.values()):
        for v in t.values():
            v["usd_saved"] = round(v["usd_saved"], 7)
    (results / "smoke_report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
