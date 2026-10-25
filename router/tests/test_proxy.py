"""Proxy tests against a fake upstream (no network)."""

import asyncio
import json

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from rr_fixtures import claude_code_request
from reprouter.proxy.server import ProxyConfig, make_app
from reprouter.router import Policy
from reprouter.transforms.base import Result, Transform

SSE = [
    b"event: message_start\ndata: " + json.dumps({"type": "message_start", "message": {
        "id": "msg_1", "usage": {"input_tokens": 12, "cache_creation_input_tokens": 3000,
                                 "cache_read_input_tokens": 9000, "output_tokens": 1}}}).encode() + b"\n\n",
    b"event: ping\ndata: {\"type\": \"ping\"}\n\n",
    b"event: content_block_delta\ndata: {\"type\":\"content_block_delta\",\"index\":0,\"delta\":{\"type\":\"text_delta\",\"text\":\"hi\"}}\n\n",
    b"event: message_delta\ndata: " + json.dumps({"type": "message_delta", "usage": {"output_tokens": 42}}).encode() + b"\n\n",
    b"event: message_stop\ndata: {\"type\":\"message_stop\"}\n\n",
]


class Shout(Transform):
    name = "shout"
    lossless = True

    def applicable(self, block, label, ctx):
        return block.type == "tool_result" and block.tool_name == "Bash"

    def apply(self, block, label, ctx):
        return Result(content="FILTERED", lossless=True)


class Explode(Transform):
    name = "explode"
    lossless = True

    def applicable(self, block, label, ctx):
        raise RuntimeError("router bug outside run()")  # run() swallows; router must still fail open

    def apply(self, block, label, ctx):
        return None


def big_bash_request():
    req = claude_code_request()
    req["messages"][4]["content"][0]["content"] = "x" * 4000
    req["metadata"] = {"user_id": "user_abc_account_xyz_session_11111111-2222-3333-4444-555555555555"}
    return req


class Upstream:
    def __init__(self):
        self.seen: list[dict] = []
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self.handle)
        self.server = TestServer(app)

    async def handle(self, request: web.Request):
        body = await request.read()
        self.seen.append({"path": request.path, "headers": dict(request.headers), "body": body})
        if request.path == "/v1/messages":
            resp = web.StreamResponse(headers={"content-type": "text/event-stream"})
            await resp.prepare(request)
            for chunk in SSE:
                await resp.write(chunk)
            await resp.write_eof()
            return resp
        return web.json_response({"input_tokens": 77})


def run(coro):
    return asyncio.run(coro)


async def with_proxy(tmp_path, cfg_kwargs, fn):
    up = Upstream()
    await up.server.start_server()
    cfg = ProxyConfig(upstream=str(up.server.make_url("")).rstrip("/"), corpus_dir=tmp_path / "corpus",
                      kill_file=tmp_path / "off", state_file=tmp_path / "sessions.json", **cfg_kwargs)
    client = TestClient(TestServer(make_app(cfg)))
    await client.start_server()
    try:
        return await fn(client, up)
    finally:
        await client.close()
        await up.server.close()


HEADERS = {"x-api-key": "sk-ant-api03-TESTSECRET", "anthropic-version": "2023-06-01",
           "anthropic-beta": "oauth-2025-04-20,extended-cache-ttl-2025-04-11",
           "x-claude-code-request-class": "main", "content-type": "application/json"}


def test_passthrough_forwards_bytes_and_streams_sse_unchanged(tmp_path):
    async def go(client, up):
        raw = json.dumps(big_bash_request()).encode()
        r = await client.post("/v1/messages", data=raw, headers=HEADERS)
        body = await r.read()
        assert r.status == 200 and r.headers["content-type"].startswith("text/event-stream")
        assert body == b"".join(SSE)
        assert up.seen[0]["body"] == raw
        h = up.seen[0]["headers"]
        assert h["x-api-key"] == "sk-ant-api03-TESTSECRET" and h["anthropic-beta"] == HEADERS["anthropic-beta"]
    run(with_proxy(tmp_path, {"mode": "passthrough"}, go))


def test_record_mode_saves_body_and_usage_but_never_credentials(tmp_path):
    async def go(client, up):
        await (await client.post("/v1/messages", data=json.dumps(big_bash_request()), headers=HEADERS)).read()
    run(with_proxy(tmp_path, {"mode": "record"}, go))
    files = list((tmp_path / "corpus").glob("*.jsonl"))
    assert len(files) == 1
    text = files[0].read_text()
    assert "TESTSECRET" not in text and "x-api-key" not in text
    rec = json.loads(text.splitlines()[0])
    assert rec["body"]["messages"][4]["content"][0]["content"] == "x" * 4000
    assert rec["usage"] == {"input_tokens": 12, "cache_creation_input_tokens": 3000,
                            "cache_read_input_tokens": 9000, "output_tokens": 42}
    assert rec["request_class"] == "main"


def first_turn(req):
    return {**req, "messages": req["messages"][:1]}


def test_on_mode_routes_main_requests(tmp_path):
    async def go(client, up):
        # the proxy sees the session from its first message, so later turns are routed
        await (await client.post("/v1/messages", data=json.dumps(first_turn(big_bash_request())),
                                 headers=HEADERS)).read()
        await (await client.post("/v1/messages", data=json.dumps(big_bash_request()), headers=HEADERS)).read()
        sent = json.loads(up.seen[1]["body"])
        assert sent["messages"][4]["content"][0]["content"] == "FILTERED"
        assert sent["messages"][2] == big_bash_request()["messages"][2]
    run(with_proxy(tmp_path, {"mode": "on", "policy": Policy(transforms=[Shout()], margin=0.0)}, go))


def test_kill_file_forces_passthrough(tmp_path):
    (tmp_path / "off").write_text("")

    async def go(client, up):
        raw = json.dumps(big_bash_request()).encode()
        await (await client.post("/v1/messages", data=raw, headers=HEADERS)).read()
        assert up.seen[0]["body"] == raw
    run(with_proxy(tmp_path, {"mode": "on", "policy": Policy(transforms=[Shout()], margin=0.0)}, go))


def test_bypass_header_and_compaction_class_pass_through(tmp_path):
    async def go(client, up):
        raw = json.dumps(big_bash_request()).encode()
        await (await client.post("/v1/messages", data=raw, headers={**HEADERS, "x-reprouter-bypass": "1"})).read()
        await (await client.post("/v1/messages", data=raw,
                                 headers={**HEADERS, "x-claude-code-request-class": "compaction"})).read()
        assert up.seen[0]["body"] == raw and up.seen[1]["body"] == raw
    run(with_proxy(tmp_path, {"mode": "on", "policy": Policy(transforms=[Shout()], margin=0.0)}, go))


def test_routing_errors_fail_open(tmp_path):
    async def go(client, up):
        raw = json.dumps(big_bash_request()).encode()
        r = await client.post("/v1/messages", data=raw, headers=HEADERS)
        assert r.status == 200
        await r.read()
        assert up.seen[0]["body"] == raw
    run(with_proxy(tmp_path, {"mode": "on", "policy": Policy(transforms=[Explode()], margin=0.0)}, go))


def test_session_first_seen_with_history_only_transforms_new_blocks_and_persists(tmp_path):
    policy = Policy(transforms=[Shout()], margin=0.0)

    async def first(client, up):
        await (await client.post("/v1/messages", data=json.dumps(big_bash_request()), headers=HEADERS)).read()
        sent = json.loads(up.seen[0]["body"])
        assert sent["messages"][4]["content"][0]["content"] == "x" * 4000  # history predates the router
    run(with_proxy(tmp_path, {"mode": "on", "policy": policy}, first))

    req = big_bash_request()
    req["messages"].append({"role": "assistant", "content": [
        {"type": "tool_use", "id": "tu_9", "name": "Bash", "input": {"command": "ls"}}]})
    req["messages"].append({"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "tu_9", "content": "y" * 4000}]})

    async def second(client, up):  # a fresh proxy process: state must come from disk
        await (await client.post("/v1/messages", data=json.dumps(req), headers=HEADERS)).read()
        sent = json.loads(up.seen[0]["body"])
        assert sent["messages"][4]["content"][0]["content"] == "x" * 4000
        assert sent["messages"][6]["content"][0]["content"] == "FILTERED"
    run(with_proxy(tmp_path, {"mode": "on", "policy": policy}, second))


def test_other_endpoints_pass_through(tmp_path):
    async def go(client, up):
        r = await client.post("/v1/messages/count_tokens", data=json.dumps(big_bash_request()), headers=HEADERS)
        assert (await r.json()) == {"input_tokens": 77}
        assert up.seen[0]["path"] == "/v1/messages/count_tokens"
    run(with_proxy(tmp_path, {"mode": "on", "policy": Policy(transforms=[Shout()], margin=0.0)}, go))


def test_canary_adds_binding_beta_and_error_mode(tmp_path):
    async def go(client, up):
        req = big_bash_request()
        req["thinking"] = {"type": "adaptive", "display": "omitted"}
        await (await client.post("/v1/messages", data=json.dumps(req), headers=HEADERS)).read()
        sent = json.loads(up.seen[0]["body"])
        assert sent["thinking"]["block_binding"] == {"prefix_mismatch_behavior": "error"}
        assert sent["thinking"]["display"] == "omitted"
        beta = up.seen[0]["headers"]["anthropic-beta"]
        assert "thinking-binding-controls-2026-08-01" in beta and "oauth-2025-04-20" in beta
    run(with_proxy(tmp_path, {"mode": "on", "policy": Policy(transforms=[]), "canary": True}, go))


def test_canary_skips_requests_without_thinking(tmp_path):
    async def go(client, up):
        raw = json.dumps(big_bash_request()).encode()
        await (await client.post("/v1/messages", data=raw, headers=HEADERS)).read()
        assert "block_binding" not in up.seen[0]["body"].decode()
    run(with_proxy(tmp_path, {"mode": "on", "policy": Policy(transforms=[]), "canary": True}, go))
