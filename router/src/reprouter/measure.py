"""Offline measurement driver: corpus -> exact token counts -> scoreboard.

  reprouter-measure scoreboard   (see cli: `reprouter scoreboard`)

Two passes over the corpus: the first collects every text the scoreboard
will need (estimates only), those are counted concurrently with the free
count_tokens endpoint into an on-disk cache, and the second pass computes
the scoreboard from exact (cached) counts.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .corpus import conversation_from_records, load_jsonl_session
from .scoreboard import REGIMES, measure, render_markdown, verdicts
from .tokens import TokenCounter, estimate_tokens


def load_conversations(root: str | os.PathLike = "~/.claude/projects") -> list[dict]:
    base = Path(os.path.expanduser(str(root)))
    convs = []
    for p in sorted(base.glob("*/*.jsonl")):
        recs = load_jsonl_session(p)
        conv = conversation_from_records(recs)
        if not conv["messages"]:
            continue
        if "system" not in conv:  # older transcripts: keep the project root for exactness rules
            cwd = next((r.get("cwd") for r in recs if r.get("cwd")), None)
            conv["system"] = [{"type": "text", "text": f"Primary working directory: {cwd}" if cwd else ""}]
            conv["tools"] = []
        convs.append({"session": p.stem, "project": p.parent.name, "body": conv})
    return convs


def claude_code_prefix_tokens(counter: TokenCounter, transcripts_root) -> int:
    """Tokens of Claude Code's own system prompt + tool definitions (latest recorded shape)."""
    base = Path(os.path.expanduser(str(transcripts_root)))
    latest = None
    for p in base.glob("*/*.jsonl"):
        for r in load_jsonl_session(p):
            if r.get("type") == "api-request-shape" and (latest is None or r.get("timestamp", "") >= latest[0]):
                latest = (r.get("timestamp", ""), r["shape"])
    if latest is None:
        return 0
    # server tools (advisor, web search, ...) are rejected by count_tokens; count custom tools only
    tools = [t for t in latest[1].get("tools") or [] if "type" not in t or t.get("type") == "custom"]
    body = {"system": latest[1].get("system"), "tools": tools,
            "messages": [{"role": "user", "content": "."}]}
    return max(0, counter.count_request(body) - counter.overhead())


def run_scoreboard(convs: list[dict], transforms: list, counter: TokenCounter, out_dir, transcripts_root,
                   fidelity: dict, lossless_exempt: set | None = None, structural: dict | None = None,
                   workers: int = 8):
    needed: set[str] = set()

    def collect(text: str) -> int:
        if text and text.strip():
            needed.add(text)
        return estimate_tokens(text)

    measure(convs, transforms, count=collect)
    counter.count_many(sorted(needed), workers=workers)
    prefix = claude_code_prefix_tokens(counter, transcripts_root)
    sb = measure(convs, transforms, count=counter.count_text, prefix_tokens=prefix)
    v = verdicts(sb, fidelity, lossless_exempt=lossless_exempt, structural=structural)

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    header = (f"# reprouter offline scoreboard\n\nGenerated {time.strftime('%Y-%m-%d %H:%M')}; token counts from "
              f"count_tokens ({counter.model}); Claude Code system+tools prefix = {prefix:,} tokens.\n\n"
              + "\n".join(f"- `{k}`: {r.label}" for k, r in REGIMES.items()) + "\n\n")
    (out / "scoreboard.md").write_text(header + render_markdown(sb, fidelity, v))
    (out / "scoreboard.json").write_text(json.dumps({
        "prefix_tokens": prefix, "sessions": sb.sessions, "tool_results": sb.tool_results,
        "tool_result_tokens": sb.tool_result_tokens, "session_weighted": sb.session_weighted,
        "transforms": {n: {"lossless": s.lossless, "applied": s.applied, "tokens_before": s.tokens_before,
                           "tokens_after": s.tokens_after, "weighted": s.weighted,
                           "by_kind": {k: dict(x) for k, x in s.by_kind.items()},
                           "verdict": v[n][0], "reason": v[n][1]} for n, s in sb.transforms.items()},
    }, indent=2))
    (out / "defaults.json").write_text(json.dumps({"enabled": [n for n in sb.transforms if v[n][0]],
                                                   "generated": time.strftime("%Y-%m-%d")}, indent=2))
    return sb
