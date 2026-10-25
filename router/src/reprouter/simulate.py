"""Turn-by-turn prompt-cache cost simulation for a sequence of requests.

Models Anthropic prompt caching as used by Claude Code:
  - prompt order is tools -> system -> messages; cache entries are created
    for the prefix ending at each block carrying cache_control;
  - a request reads the longest previously cached prefix that is an exact
    prefix of it (searched up to ``lookback`` blocks before each of its
    breakpoints) and writes the rest up to its last breakpoint;
  - entries expire ``ttl`` after their last write/read (5m or 1h);
  - prefixes shorter than ``min_cacheable`` tokens are not cached.

Weighted cost is in "input-token equivalents": uncached x1, write x1.25 (5m)
or x2 (1h), read x0.1 (x0.05 on Opus/Sonnet 5.5, x0.025 on Fable 5.1).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Callable

TTL_SECONDS = {"5m": 300, "1h": 3600}


@dataclass(frozen=True)
class CachePricing:
    read: float = 0.1
    write_5m: float = 1.25
    write_1h: float = 2.0

    @classmethod
    def for_model(cls, model: str) -> "CachePricing":
        m = (model or "").lower()
        if "fable-5-1" in m or "mythos-5-1" in m:
            return cls(read=0.025)
        if "opus-5-5" in m or "sonnet-5-5" in m:
            return cls(read=0.05)
        return cls(read=0.1)


@dataclass
class TurnCost:
    t: float
    total: int
    uncached: int
    write: int
    read: int
    weighted: float


def _seq(req: dict, count: Callable[[str], int]) -> list[tuple[str, int, str | None]]:
    """[(block canonical key, tokens, ttl-if-breakpoint)] in prompt order."""
    out = []

    def add(block: dict, text: str):
        b = {k: v for k, v in block.items() if k != "cache_control"} if isinstance(block, dict) else block
        key = json.dumps(b, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        cc = block.get("cache_control") if isinstance(block, dict) else None
        ttl = ("1h" if cc.get("ttl") == "1h" else "5m") if cc else None
        out.append((key, count(text), ttl))

    for t in req.get("tools") or []:
        add(t, json.dumps(t, sort_keys=True))
    system = req.get("system")
    if isinstance(system, str):
        add({"type": "text", "text": system}, system)
    else:
        for b in system or []:
            add(b, b.get("text", ""))
    for m in req.get("messages") or []:
        c = m.get("content")
        if isinstance(c, str):
            add({"role": m.get("role"), "text": c}, c)
            continue
        for b in c or []:
            if b.get("type") == "text":
                text = b.get("text", "")
            elif b.get("type") == "tool_result":
                cc = b.get("content")
                text = cc if isinstance(cc, str) else "\n".join(x.get("text", "") for x in cc or [] if isinstance(x, dict))
            elif b.get("type") == "tool_use":
                text = json.dumps(b.get("input") or {}, sort_keys=True)
            elif b.get("type") == "thinking":
                text = b.get("thinking", "")
            else:
                text = json.dumps(b, sort_keys=True)
            add({"role": m.get("role"), **b}, text)
    return out


def simulate_session(requests: list[tuple[float, dict]], count: Callable[[str], int],
                     min_cacheable: int = 512, lookback: int = 20) -> list[TurnCost]:
    entries: dict[str, tuple[float, int]] = {}  # prefix hash -> (expiry, ttl seconds)
    results: list[TurnCost] = []
    for t, req in requests:
        seq = _seq(req, count)
        pricing = CachePricing.for_model(req.get("model", ""))
        hashes, cum = [], []
        h = hashlib.sha256()
        total = 0
        for key, n, _ in seq:
            h.update(key.encode())
            h.update(b"\x00")
            hashes.append(h.hexdigest())
            total += n
            cum.append(total)
        bps = [i for i, (_, _, ttl) in enumerate(seq) if ttl]
        # longest live cached prefix reachable from a breakpoint within the lookback window
        read_idx = -1
        for bp in bps:
            for i in range(bp, max(-1, bp - lookback), -1):
                e = entries.get(hashes[i])
                if e and e[0] >= t:
                    read_idx = max(read_idx, i)
                    break
        read = cum[read_idx] if read_idx >= 0 else 0
        if read_idx >= 0:
            exp, ttl_s = entries[hashes[read_idx]]
            entries[hashes[read_idx]] = (t + ttl_s, ttl_s)
        last_bp = max(bps) if bps else -1
        cached_upto = cum[last_bp] if last_bp >= 0 else 0
        write, write_ttl = 0, "5m"
        if last_bp >= 0 and cached_upto >= min_cacheable:
            write = max(0, cached_upto - read)
            write_ttl = seq[last_bp][2]
            for bp in bps:
                if cum[bp] >= min_cacheable:
                    ttl_s = TTL_SECONDS[seq[bp][2]]
                    entries[hashes[bp]] = (t + ttl_s, ttl_s)
        else:
            cached_upto = read
        uncached = total - max(cached_upto, read)
        w = pricing.write_1h if write_ttl == "1h" else pricing.write_5m
        results.append(TurnCost(t, total, uncached, write, read, uncached + write * w + read * pricing.read))
    return results
