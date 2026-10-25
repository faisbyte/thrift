"""Token estimation (fast, deterministic, used online by the router) and exact
counting via the free count_tokens endpoint (used offline, cached on disk).

The cache stores only hashes and counts, never content.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

CHARS_PER_TOKEN = 2.7  # measured with count_tokens on Haiku/Sonnet 5.5: prose 2.5, code 2.7, paths 1.8
HIGH_RES_MAX_EDGE = 2576
HIGH_RES_MAX_TOKENS = 4784
PATCH = 28


def estimate_tokens(text: str) -> int:
    return math.ceil(len(text) / CHARS_PER_TOKEN) if text else 0


def image_tokens(width: int, height: int) -> int:
    """Visual tokens for Claude 4.7+ models: ceil(w/28) x ceil(h/28), after the
    API downsamples to a 2576px long edge and at most 4784 tokens."""
    w, h = float(width), float(height)
    s = min(1.0, HIGH_RES_MAX_EDGE / max(w, h))
    w, h = w * s, h * s
    tokens = math.ceil(w / PATCH) * math.ceil(h / PATCH)
    while tokens > HIGH_RES_MAX_TOKENS:
        f = math.sqrt(HIGH_RES_MAX_TOKENS / tokens) * 0.999
        w, h = w * f, h * f
        tokens = math.ceil(w / PATCH) * math.ceil(h / PATCH)
    return tokens


class TokenCounter:
    """Exact token counts via an injected count function (body -> input_tokens)."""

    def __init__(self, count_fn: Callable[[dict], int], cache_path: str | os.PathLike, model: str):
        self.count_fn = count_fn
        self.cache_path = Path(cache_path)
        self.model = model
        self._cache: dict[str, int] = {}
        if self.cache_path.exists():
            for line in self.cache_path.read_text().splitlines():
                if line.strip():
                    e = json.loads(line)
                    self._cache[e["k"]] = e["n"]
        self._overhead: int | None = None
        self._lock = threading.Lock()

    def _key(self, kind: str, payload) -> str:
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(f"{self.model}\0{kind}\0{blob}".encode()).hexdigest()

    def _cached(self, kind: str, payload, body: dict) -> int:
        k = self._key(kind, payload)
        if k in self._cache:
            return self._cache[k]
        n = int(self.count_fn(body))
        with self._lock:
            if k not in self._cache:
                self._cache[k] = n
                self.cache_path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.cache_path, "a") as f:
                    f.write(json.dumps({"k": k, "n": n}) + "\n")
        return self._cache[k]

    def _user_body(self, content) -> dict:
        return {"model": self.model, "messages": [{"role": "user", "content": content}]}

    def overhead(self) -> int:
        if self._overhead is None:
            self._overhead = self._cached("content", ".", self._user_body("."))
        return self._overhead

    def count_content(self, content) -> int:
        """Tokens for a user message's content (str or content blocks), minus request overhead."""
        return max(0, self._cached("content", content, self._user_body(content)) - self.overhead())

    def count_text(self, text: str) -> int:
        return self.count_content(text)

    def count_many(self, contents: list, workers: int = 8) -> list[int]:
        """Count many contents concurrently (warms the cache), in input order."""
        self.overhead()
        unique = list({json.dumps(c, sort_keys=True): c for c in contents}.values())
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(self.count_content, unique))
        return [self.count_content(c) for c in contents]

    def count_request(self, body: dict) -> int:
        body = {k: v for k, v in body.items() if k in ("model", "system", "tools", "messages", "thinking")}
        body.setdefault("model", self.model)
        return self._cached("request", body, body)


def load_dotenv(path: str | os.PathLike | None = None) -> None:
    """Load KEY=VALUE lines from the repo .env without overriding the environment."""
    p = Path(path) if path else Path(__file__).resolve().parents[3] / ".env"
    if not p.is_file():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.removeprefix("export ").partition("=")
        k, v = k.strip(), v.strip().strip("'\"")
        if k and v and k not in os.environ:
            os.environ[k] = v


class AnthropicCountClient:
    """POST /v1/messages/count_tokens with the API key from the environment.

    count_tokens is free (RPM-limited). The key is never logged or repr'd.
    """

    URL = "https://api.anthropic.com/v1/messages/count_tokens"

    def __init__(self, max_retries: int = 6):
        # entry points call load_dotenv(); the client only reads the environment
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set (put it in .env); count_tokens needs it")
        self.__key = key
        self.max_retries = max_retries

    def __repr__(self) -> str:
        return "AnthropicCountClient(key=<redacted>)"

    def __call__(self, body: dict) -> int:
        data = json.dumps(body).encode()
        for attempt in range(self.max_retries):
            req = urllib.request.Request(self.URL, data=data, method="POST", headers={
                "x-api-key": self.__key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    return int(json.loads(resp.read())["input_tokens"])
            except urllib.error.HTTPError as e:
                if e.code in (429, 500, 502, 503, 529) and attempt < self.max_retries - 1:
                    time.sleep(min(30, 2 ** attempt))
                    continue
                detail = e.read().decode(errors="replace")[:300]
                raise RuntimeError(f"count_tokens failed: HTTP {e.code}: {detail}") from None
        raise RuntimeError("count_tokens: retries exhausted")
