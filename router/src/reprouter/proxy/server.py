"""Local HTTP proxy between Claude Code and the Anthropic API.

Set ``ANTHROPIC_BASE_URL=http://127.0.0.1:<port>``. Every request is forwarded
with its headers intact (API key or subscription OAuth) and the response is
streamed back chunk by chunk, never buffered (Claude Code stalls on buffered
SSE).

Modes: ``off``/``passthrough`` forward untouched; ``record`` also writes the
request body and response usage to the corpus (credentials never written);
``on`` routes main/subagent /v1/messages requests through the representation
router (and records). The kill file and the ``x-reprouter-bypass`` header
force passthrough per request. Any routing error fails open: the original
bytes are forwarded.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp
from aiohttp import web

from ..router import Policy, route

log = logging.getLogger("reprouter.proxy")

HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer",
              "transfer-encoding", "upgrade", "host", "content-length"}
SECRET_HEADERS = {"x-api-key", "authorization", "cookie", "proxy-authorization"}
ROUTED_CLASSES = {"main", "subagent", ""}
_SESSION_RE = re.compile(r"session_([0-9a-fA-F-]{36})")


@dataclass
class ProxyConfig:
    upstream: str = "https://api.anthropic.com"
    mode: str = "passthrough"  # off | passthrough | record | on
    policy: Policy = field(default_factory=Policy)
    record_when_on: bool = True
    corpus_dir: Path = Path("corpus/proxy")
    kill_file: Path = Path(os.path.expanduser("~/.reprouter/off"))
    state_file: Path = Path(os.path.expanduser("~/.reprouter/sessions.json"))


class SessionState:
    """min_msg_index per session, persisted so a restart never re-decides history."""

    def __init__(self, path: Path):
        self.path = Path(path)
        try:
            self.data: dict[str, int] = json.loads(self.path.read_text())
        except (OSError, ValueError):
            self.data = {}

    def min_index(self, key: str, messages: list) -> int:
        if key not in self.data:
            has_history = any(m.get("role") == "assistant" for m in messages)
            self.data[key] = len(messages) if has_history else 0
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data))
            tmp.replace(self.path)
        return self.data[key]


def session_key(body: dict) -> str:
    uid = ((body.get("metadata") or {}).get("user_id")) or ""
    m = _SESSION_RE.search(uid)
    if m:
        return m.group(1)
    first = (body.get("messages") or [{}])[0]
    return "h" + hashlib.sha256(json.dumps(first, sort_keys=True).encode()).hexdigest()[:24]


def parse_sse_usage(data: bytes) -> dict:
    usage: dict = {}
    for line in data.decode("utf-8", "replace").splitlines():
        if not line.startswith("data:"):
            continue
        try:
            ev = json.loads(line[5:].strip())
        except ValueError:
            continue
        if ev.get("type") == "message_start":
            usage.update((ev.get("message") or {}).get("usage") or {})
        elif ev.get("type") == "message_delta" and ev.get("usage"):
            usage.update(ev["usage"])
    return usage


class Proxy:
    def __init__(self, cfg: ProxyConfig):
        self.cfg = cfg
        self.sessions = SessionState(cfg.state_file)
        self.http: aiohttp.ClientSession | None = None

    async def startup(self, app):
        self.http = aiohttp.ClientSession(auto_decompress=False, timeout=aiohttp.ClientTimeout(total=None))

    async def cleanup(self, app):
        if self.http:
            await self.http.close()

    def _active(self, request: web.Request) -> bool:
        if self.cfg.mode in ("off", "passthrough"):
            return False
        if request.headers.get("x-reprouter-bypass"):
            return False
        return not Path(self.cfg.kill_file).exists()

    def _record(self, entry: dict) -> None:
        d = Path(self.cfg.corpus_dir)
        d.mkdir(parents=True, exist_ok=True)
        day = time.strftime("%Y%m%d")
        with open(d / f"{day}-{entry['session'][:12]}.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def _route(self, body: dict) -> tuple[dict, list]:
        key = session_key(body)
        min_idx = self.sessions.min_index(key, body.get("messages") or [])
        return route(body, self.cfg.policy, min_msg_index=min_idx)

    async def handle(self, request: web.Request) -> web.StreamResponse:
        raw = await request.read()
        out = raw
        active = self._active(request)
        is_messages = request.method == "POST" and request.path.rstrip("/").endswith("/v1/messages")
        rclass = request.headers.get("x-claude-code-request-class", "")
        body = None
        decisions: list = []
        recording = active and is_messages and (self.cfg.mode == "record" or self.cfg.record_when_on)
        if active and is_messages:
            try:
                body = json.loads(raw)
            except ValueError:
                body = None
        if body is not None and self.cfg.mode == "on" and rclass in ROUTED_CLASSES:
            try:
                routed, decisions = await asyncio.to_thread(self._route, body)
                if any(d.transform for d in decisions):
                    out = json.dumps(routed, ensure_ascii=False, separators=(",", ":")).encode()
            except Exception:
                log.exception("routing failed; forwarding original request")
                out, decisions = raw, []

        headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_BY_HOP}
        url = self.cfg.upstream + request.rel_url.path_qs
        assert self.http is not None
        async with self.http.request(request.method, url, data=out, headers=headers) as up:
            resp = web.StreamResponse(status=up.status, headers={
                k: v for k, v in up.headers.items() if k.lower() not in HOP_BY_HOP})
            await resp.prepare(request)
            captured = bytearray() if recording else None
            async for chunk in up.content.iter_any():
                await resp.write(chunk)
                if captured is not None:
                    captured.extend(chunk)
            await resp.write_eof()

        if recording and body is not None:
            try:
                self._record({
                    "ts": time.time(), "path": request.path, "request_class": rclass, "session": session_key(body),
                    "status": up.status, "body": body,
                    "usage": parse_sse_usage(bytes(captured or b"")) if "encoding" not in str(up.headers) else {},
                    "decisions": [{"path": list(d.path), "kind": d.kind, "transform": d.transform,
                                   "before": d.tokens_before, "after": d.tokens_after} for d in decisions
                                  if d.transform],
                })
            except Exception:
                log.exception("recording failed")
        return resp


def make_app(cfg: ProxyConfig) -> web.Application:
    proxy = Proxy(cfg)
    app = web.Application(client_max_size=64 * 1024 * 1024)
    app.on_startup.append(proxy.startup)
    app.on_cleanup.append(proxy.cleanup)
    app.router.add_route("*", "/{tail:.*}", proxy.handle)
    return app
