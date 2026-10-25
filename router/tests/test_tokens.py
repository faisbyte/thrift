import pytest

from reprouter.tokens import TokenCounter, estimate_tokens, image_tokens


class FakeCountAPI:
    """Stands in for POST /v1/messages/count_tokens: 1 token per 4 chars + 7 overhead."""

    def __init__(self):
        self.calls = 0

    def __call__(self, body: dict) -> int:
        self.calls += 1
        content = body["messages"][0]["content"]
        text = content if isinstance(content, str) else "".join(c.get("text", "") for c in content)
        return 7 + len(text) // 4


def test_estimate_is_deterministic_and_monotonic():
    assert estimate_tokens("") == 0
    assert estimate_tokens("a" * 100) == estimate_tokens("a" * 100)
    assert estimate_tokens("a" * 1000) > estimate_tokens("a" * 100)


def test_image_tokens_use_28px_patches_and_cap():
    assert image_tokens(1568, 728) == 56 * 26
    assert image_tokens(28, 28) == 1
    assert image_tokens(29, 28) == 2
    big = image_tokens(8000, 8000)  # downsampled until it fits the high-res tier cap
    assert 4000 < big <= 4784


def test_counter_subtracts_request_overhead(tmp_path):
    api = FakeCountAPI()
    tc = TokenCounter(api, cache_path=tmp_path / "c.jsonl", model="claude-haiku-5-5")
    assert tc.count_text("x" * 400) == 100
    assert tc.count_text("x" * 40) == 10


def test_counter_caches_by_model_and_text(tmp_path):
    api = FakeCountAPI()
    tc = TokenCounter(api, cache_path=tmp_path / "c.jsonl", model="claude-haiku-5-5")
    tc.count_text("hello world " * 20)
    calls = api.calls
    tc.count_text("hello world " * 20)
    assert api.calls == calls
    # persists across instances
    tc2 = TokenCounter(api, cache_path=tmp_path / "c.jsonl", model="claude-haiku-5-5")
    tc2.count_text("hello world " * 20)
    assert api.calls == calls
    # a different model is a different cache key
    tc3 = TokenCounter(api, cache_path=tmp_path / "c.jsonl", model="claude-sonnet-5-5")
    tc3.count_text("hello world " * 20)
    assert api.calls > calls


def test_count_request_passes_body_through(tmp_path):
    api = FakeCountAPI()
    tc = TokenCounter(api, cache_path=tmp_path / "c.jsonl", model="claude-haiku-5-5")
    body = {"model": "claude-haiku-5-5", "messages": [{"role": "user", "content": "y" * 80}]}
    assert tc.count_request(body) == 27


def test_cache_file_never_contains_text(tmp_path):
    api = FakeCountAPI()
    tc = TokenCounter(api, cache_path=tmp_path / "c.jsonl", model="claude-haiku-5-5")
    tc.count_text("SECRET-PROJECT-CONTENT " * 10)
    assert "SECRET-PROJECT-CONTENT" not in (tmp_path / "c.jsonl").read_text()


def test_api_client_requires_key_and_never_exposes_it(monkeypatch):
    from reprouter.tokens import AnthropicCountClient

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(RuntimeError):
        AnthropicCountClient()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-SECRETVALUE")
    c = AnthropicCountClient()
    assert "SECRETVALUE" not in repr(c) and "SECRETVALUE" not in str(vars(c).get("_repr", ""))


def test_count_many_warms_cache_concurrently(tmp_path):
    api = FakeCountAPI()
    tc = TokenCounter(api, cache_path=tmp_path / "c.jsonl", model="claude-haiku-5-5")
    texts = [f"text number {i} " * 20 for i in range(30)] + ["text number 0 " * 20]  # one duplicate
    counts = tc.count_many(texts, workers=4)
    assert counts == [tc.count_text(t) for t in texts]
    calls = api.calls
    tc.count_many(texts, workers=4)
    assert api.calls == calls  # all cached now
    assert len((tmp_path / "c.jsonl").read_text().splitlines()) == 31  # overhead + 30 unique texts


def test_empty_text_counts_zero_without_calling_the_api(tmp_path):
    api = FakeCountAPI()
    tc = TokenCounter(api, cache_path=tmp_path / "c.jsonl", model="claude-haiku-5-5")
    assert tc.count_text("") == 0 and tc.count_text("   \n ") == 0
    assert api.calls == 0
