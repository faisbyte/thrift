"""Estimated API cost from token counts, using published per-token list prices.

Used for subscription runs (where no API bill exists) and as a cross-check on
Claude Code's own `total_cost_usd` for API-key runs.

Prices: USD per million tokens, Anthropic first-party API list prices from
platform.claude.com/docs/en/about-claude/pricing (checked 2026-10-25). Cache writes are 1.25x
input for the 5-minute TTL and 2x for the 1-hour TTL; cache reads are a
per-model fraction of input. Approximations: Haiku 5.5's long-prompt tier
(requests whose prompt exceeds 100K tokens bill at $0.50 / $2.50) is ignored -
runs record `max_prompt_tokens` so the report can flag it - and the 1.1x
US-only inference multiplier is not applied.
"""

from __future__ import annotations

PRICES_AS_OF = "2026-10-25"

# family/model key -> (input, output, cache_read) USD per MTok
PRICES: dict[str, tuple[float, float, float]] = {
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
    "claude-sonnet-5-5": (2.00, 10.00, 0.10),
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    "claude-fable-5-1": (10.00, 50.00, 0.25),
    # previous generation, in case a run pins one
    "claude-sonnet-5": (2.00, 10.00, 0.20),
    "claude-opus-5": (5.00, 25.00, 0.50),
    "claude-haiku-4-5": (1.00, 5.00, 0.10),
}
CACHE_WRITE_5M = 1.25
CACHE_WRITE_1H = 2.0


def price_for(model: str) -> tuple[float, float, float] | None:
    """Look up prices for a model id as reported by Claude Code (exact id, a
    date-suffixed id, or a bare alias such as "haiku")."""
    m = (model or "").lower()
    for key in sorted(PRICES, key=len, reverse=True):  # longest match first: sonnet-5-5 before sonnet-5
        if m == key or m.startswith(key + "-") or m.startswith(key + "["):
            return PRICES[key]
    for alias, key in (("haiku", "claude-haiku-5-5"), ("sonnet", "claude-sonnet-5-5"),
                       ("opus", "claude-opus-5-5"), ("fable", "claude-fable-5-1")):
        if alias in m and not any(c.isdigit() for c in m.replace(alias, "")):
            return PRICES[key]
    return None


def estimate_cost(model_usage: dict, cache_1h_fraction: float = 0.0) -> float | None:
    """Sum the estimated cost over a result event's `modelUsage` map.

    `cache_1h_fraction` is the share of cache-write tokens written with the
    1-hour TTL (from per-message `usage.cache_creation`); the rest are priced
    as 5-minute writes. Returns None if any model is unknown.
    """
    total = 0.0
    for model, u in (model_usage or {}).items():
        p = price_for(model)
        if p is None:
            return None
        p_in, p_out, p_read = p
        writes = u.get("cacheCreationInputTokens") or 0
        write_mult = CACHE_WRITE_5M * (1 - cache_1h_fraction) + CACHE_WRITE_1H * cache_1h_fraction
        total += (
            (u.get("inputTokens") or 0) * p_in
            + writes * p_in * write_mult
            + (u.get("cacheReadInputTokens") or 0) * p_read
            + (u.get("outputTokens") or 0) * p_out
        ) / 1_000_000
    return round(total, 6)
