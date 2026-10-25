import copy

import pytest

from reprouter.simulate import CachePricing, simulate_session


def blocks_req(texts, marker_at=None, ttl="5m"):
    """A request whose messages are single-text user/assistant turns; marker on message index marker_at."""
    msgs = []
    for i, t in enumerate(texts):
        b = {"type": "text", "text": t}
        if marker_at is not None and i == marker_at:
            b["cache_control"] = {"type": "ephemeral", **({"ttl": "1h"} if ttl == "1h" else {})}
        msgs.append({"role": "user" if i % 2 == 0 else "assistant", "content": [b]})
    return {"model": "claude-haiku-5-5", "system": [{"type": "text", "text": "S" * 270}], "messages": msgs}


def tok(text):  # deterministic token function for tests: 1 token per 10 chars
    return len(text) // 10


A, B, C, D = "a" * 1000, "b" * 1000, "c" * 1000, "d" * 1000


def test_first_request_writes_prefix_up_to_marker():
    r = simulate_session([(0.0, blocks_req([A, B], marker_at=1))], count=tok, min_cacheable=0)[0]
    assert r.read == 0 and r.write == 27 + 100 + 100 and r.uncached == 0


def test_next_request_reads_previous_prefix_and_writes_delta():
    reqs = [(0.0, blocks_req([A, B], marker_at=1)), (10.0, blocks_req([A, B, C, D], marker_at=3))]
    r1, r2 = simulate_session(reqs, count=tok, min_cacheable=0)
    assert r2.read == 227 and r2.write == 200 and r2.uncached == 0


def test_tokens_after_last_marker_are_uncached():
    r = simulate_session([(0.0, blocks_req([A, B, C], marker_at=1))], count=tok, min_cacheable=0)[0]
    assert r.write == 227 and r.uncached == 100


def test_changing_an_earlier_block_invalidates_the_cache_from_there():
    first = blocks_req([A, B], marker_at=1)
    second = blocks_req([A, "B" * 1000, C, D], marker_at=3)
    r1, r2 = simulate_session([(0.0, first), (10.0, second)], count=tok, min_cacheable=0)
    assert r2.read == 0  # no earlier breakpoint prefix matches (only one marker, after the change)
    assert r2.write == 27 + 400


def test_ttl_expiry_means_no_read():
    reqs = [(0.0, blocks_req([A, B], marker_at=1)), (301.0, blocks_req([A, B, C, D], marker_at=3))]
    r2 = simulate_session(reqs, count=tok, min_cacheable=0)[1]
    assert r2.read == 0 and r2.write == 427


def test_one_hour_ttl_survives_a_long_pause():
    reqs = [(0.0, blocks_req([A, B], marker_at=1, ttl="1h")), (1800.0, blocks_req([A, B, C, D], marker_at=3, ttl="1h"))]
    r2 = simulate_session(reqs, count=tok, min_cacheable=0)[1]
    assert r2.read == 227


def test_reads_refresh_ttl():
    reqs = [(0.0, blocks_req([A, B], marker_at=1)), (200.0, blocks_req([A, B, C], marker_at=1)),
            (450.0, blocks_req([A, B, C, D], marker_at=3))]
    r3 = simulate_session(reqs, count=tok, min_cacheable=0)[2]
    assert r3.read == 227  # the read at t=200 kept the entry alive until t=500


def test_cost_weighting_uses_ttl_and_model_read_price():
    p = CachePricing.for_model("claude-sonnet-5-5")
    assert p.read == 0.05 and p.write_5m == 1.25 and p.write_1h == 2.0
    assert CachePricing.for_model("claude-haiku-5-5").read == 0.1
    reqs = [(0.0, blocks_req([A, B], marker_at=1, ttl="1h"))]
    r = simulate_session(reqs, count=tok, min_cacheable=0)[0]
    assert r.weighted == pytest.approx(227 * 2.0)


def test_identical_requests_are_fully_read():
    req = blocks_req([A, B], marker_at=1)
    r2 = simulate_session([(0.0, req), (5.0, copy.deepcopy(req))], count=tok, min_cacheable=0)[1]
    assert r2.read == 227 and r2.write == 0


def test_prefixes_below_the_minimum_are_not_cached():
    reqs = [(0.0, blocks_req([A, B], marker_at=1)), (10.0, blocks_req([A, B, C, D], marker_at=3))]
    r1, r2 = simulate_session(reqs, count=tok, min_cacheable=512)
    assert r1.write == 0 and r1.uncached == 227 and r2.read == 0  # 227 < 512: nothing cached at bp 1
