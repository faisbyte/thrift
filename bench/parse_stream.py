"""Turn a `claude -p --output-format stream-json --verbose` transcript into metrics."""

from __future__ import annotations

import json
import shlex
from collections import Counter
from pathlib import Path

THRIFT_TOOL_PREFIX = "mcp__plugin_thrift_thrift__"
READ_COMMANDS = {"cat", "head", "tail", "sed", "less", "nl"}


def _bash_read_paths(command: str) -> list[str]:
    """File arguments of a simple cat/head/tail/sed read (first command of a pipeline only)."""
    try:
        toks = shlex.split(command.split("|")[0].split("&&")[0])
    except ValueError:
        return []
    if not toks or toks[0] not in READ_COMMANDS:
        return []
    return [t for t in toks[1:] if not t.startswith("-") and ("/" in t or "." in t)]


def _same_file(written: str, path: str) -> bool:
    return written == path or written.endswith("/" + path.lstrip("./"))


def _content_len(content) -> int:
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        return sum(len(c.get("text", "")) if isinstance(c, dict) else len(str(c)) for c in content)
    return len(json.dumps(content)) if content is not None else 0


def parse_stream(lines) -> dict:
    """Parse stream-json lines (iterable of str) into a flat metrics dict."""
    init: dict = {}
    result: dict = {}
    tool_calls: Counter = Counter()
    tool_result_chars: Counter = Counter()
    id_to_tool: dict[str, str] = {}
    files_read: set[str] = set()
    reads = reads_ranged = 0
    written: set[str] = set()
    reads_after_write = 0
    errors: list[str] = []
    seen_msgs: set[str] = set()
    cw_5m = cw_1h = 0
    max_prompt = 0

    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            ev = json.loads(raw)
        except json.JSONDecodeError:
            continue
        et = ev.get("type")
        if et == "system" and ev.get("subtype") == "init":
            init = ev
        elif et == "assistant":
            if ev.get("parent_tool_use_id"):
                continue  # subagent traffic (disallowed in the bench, but be safe)
            msg = ev.get("message") or {}
            mid = msg.get("id")
            if mid and mid not in seen_msgs:  # one API response can span several events
                seen_msgs.add(mid)
                u = msg.get("usage") or {}
                cc = u.get("cache_creation") or {}
                cw_5m += cc.get("ephemeral_5m_input_tokens") or 0
                cw_1h += cc.get("ephemeral_1h_input_tokens") or 0
                max_prompt = max(max_prompt, (u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0)
                                 + (u.get("cache_creation_input_tokens") or 0))
            for block in msg.get("content") or []:
                if block.get("type") != "tool_use":
                    continue
                name = block.get("name", "?")
                tool_calls[name] += 1
                id_to_tool[block.get("id", "")] = name
                inp = block.get("input") or {}
                read_paths = ([inp["file_path"]] if name == "Read" and inp.get("file_path") else
                              _bash_read_paths(inp.get("command") or "") if name == "Bash" else [])
                reads_after_write += sum(1 for rp in read_paths if any(_same_file(w, rp) for w in written))
                if name in ("Write", "Edit", "MultiEdit") and inp.get("file_path"):
                    written.add(inp["file_path"])
                if name == "Read":
                    reads += 1
                    if inp.get("file_path"):
                        files_read.add(inp["file_path"])
                    if inp.get("offset") is not None or inp.get("limit") is not None:
                        reads_ranged += 1
        elif et == "user":
            for block in (ev.get("message") or {}).get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    name = id_to_tool.get(block.get("tool_use_id", ""), "?")
                    tool_result_chars[name] += _content_len(block.get("content"))
        elif et == "result":
            result = ev

    usage = result.get("usage") or {}
    model_usage = result.get("modelUsage") or {}
    in_tok = usage.get("input_tokens", 0) or 0
    cache_w = usage.get("cache_creation_input_tokens", 0) or 0
    cache_r = usage.get("cache_read_input_tokens", 0) or 0
    out_tok = usage.get("output_tokens", 0) or 0
    mu_cost = sum((m.get("costUSD") or 0) for m in model_usage.values())
    mu_in = sum((m.get("inputTokens") or 0) + (m.get("cacheReadInputTokens") or 0)
                + (m.get("cacheCreationInputTokens") or 0) for m in model_usage.values())
    mu_out = sum((m.get("outputTokens") or 0) for m in model_usage.values())

    thrift_calls = sum(n for t, n in tool_calls.items() if t.startswith(THRIFT_TOOL_PREFIX))
    if result.get("is_error"):
        errors.append(str(result.get("subtype")))

    return {
        "answer": result.get("result", ""),
        "subtype": result.get("subtype"),
        "is_error": bool(result.get("is_error")),
        "num_turns": result.get("num_turns"),
        "duration_ms": result.get("duration_ms"),
        "duration_api_ms": result.get("duration_api_ms"),
        "cost_usd": result.get("total_cost_usd"),
        "cost_usd_model_usage": round(mu_cost, 6),
        "input_tokens": in_tok,
        "cache_write_tokens": cache_w,
        "cache_read_tokens": cache_r,
        "input_total": in_tok + cache_w + cache_r,
        "output_tokens": out_tok,
        "input_total_all_models": mu_in,
        "output_tokens_all_models": mu_out,
        "tool_calls": dict(tool_calls),
        "tool_result_chars": dict(tool_result_chars),
        "tool_result_chars_total": sum(tool_result_chars.values()),
        "reads": reads,
        "reads_ranged": reads_ranged,
        # re-reads of files this run already wrote/edited (what tool_input_elide could cause)
        "reads_after_write": reads_after_write,
        "files_read": len(files_read),
        "files_read_list": sorted(files_read),
        "greps": tool_calls.get("Grep", 0),
        "globs": tool_calls.get("Glob", 0),
        "bash_calls": tool_calls.get("Bash", 0),
        "edits": tool_calls.get("Edit", 0) + tool_calls.get("Write", 0) + tool_calls.get("MultiEdit", 0),
        "thrift_calls": thrift_calls,
        "init_tools": init.get("tools", []),
        "init_mcp_servers": init.get("mcp_servers", []),
        "init_plugins": init.get("plugins", []),
        "init_plugin_errors": init.get("plugin_errors", []),
        "model": init.get("model"),
        "errors": errors,
        "cache_write_1h_fraction": round(cw_1h / (cw_5m + cw_1h), 4) if (cw_5m + cw_1h) else 0.0,
        "max_prompt_tokens": max_prompt,
        "model_usage": model_usage,
    }


def parse_file(path: str | Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return parse_stream(f)


if __name__ == "__main__":
    import sys

    print(json.dumps(parse_file(sys.argv[1]), indent=2))
