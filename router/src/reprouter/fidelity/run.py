"""Run fidelity probes: each probe is asked once against the original tool
output and once per transform against the transformed output, embedded in a
realistic tool-call conversation. Paired results feed fidelity.score.

Every paid run is gated by the spend ledger before the first call and
recorded after.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

from ..request import parse_blocks
from ..router import Policy, content_tokens, route
from ..spend import Ledger
from ..tokens import estimate_tokens
from .samples import Sample
from .score import is_correct

PROBE_SYSTEM = ("You are a careful assistant. Answer the user's question using only the tool outputs in this "
                "conversation. Reply with only the answer value, no explanation. If the answer is not in the "
                "tool outputs, reply exactly NOT PRESENT.")
INSTRUCTION = "Reply with only the answer value, or NOT PRESENT if it is not in the tool output."
TOOLS = [
    {"name": "Bash", "description": "Run a shell command.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "Read", "description": "Read a file.",
     "input_schema": {"type": "object", "properties": {"file_path": {"type": "string"}}, "required": ["file_path"]}},
    {"name": "Grep", "description": "Search files.",
     "input_schema": {"type": "object", "properties": {"pattern": {"type": "string"}, "path": {"type": "string"}},
                      "required": ["pattern"]}},
]
# USD per million tokens: input, output, cache write (5m), cache read
PRICES = {"haiku": (0.10, 0.50, 0.125, 0.01), "sonnet": (2.0, 10.0, 2.5, 0.10)}


def _price(model: str):
    return PRICES["sonnet" if "sonnet" in model else "haiku"]


def _messages(sample: Sample, content, question: str | None) -> list[dict]:
    msgs = [{"role": "user", "content": [{"type": "text", "text": "Please investigate this for me."}]}]
    for i, p in enumerate(sample.prior):
        tid = f"toolu_prior_{i}"
        msgs.append({"role": "assistant", "content": [
            {"type": "tool_use", "id": tid, "name": p["tool_name"], "input": p["tool_input"]}]})
        msgs.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid, "content": p["text"]}]})
    msgs.append({"role": "assistant", "content": [
        {"type": "tool_use", "id": "toolu_current", "name": sample.tool_name, "input": sample.tool_input}]})
    last = [{"type": "tool_result", "tool_use_id": "toolu_current", "content": content,
             "cache_control": {"type": "ephemeral"}}]
    if question is not None:
        last.append({"type": "text", "text": f"{question}\n{INSTRUCTION}"})
    msgs.append({"role": "user", "content": last})
    return msgs


def build_body(sample: Sample, content, question: str, model: str = "claude-haiku-5-5") -> dict:
    return {"model": model, "max_tokens": 120, "system": PROBE_SYSTEM, "tools": TOOLS,
            "tool_choice": {"type": "none"}, "thinking": {"type": "disabled"},
            "messages": _messages(sample, content, question)}


def transformed_content(sample: Sample, transform):
    """The current tool result after applying only ``transform`` (fidelity rules relaxed:
    we measure the raw transform). None when it does not apply."""
    req = {"model": "claude-haiku-5-5",
           "system": [{"type": "text", "text": f"Primary working directory: {sample.project_root}"}],
           "messages": _messages(sample, sample.text, None)}
    policy = Policy(transforms=[transform], margin=0.0, allow_nontext=True,
                    lossy_blocked=frozenset(), nontext_blocked=frozenset())
    routed, decisions = route(req, policy)
    path = next(b.path for b in parse_blocks(req) if b.type == "tool_result" and b.tool_use_id == "toolu_current")
    d = next(d for d in decisions if d.path == path)
    if not d.transform:
        return None
    return routed["messages"][path[1]]["content"][path[3]]["content"]


def _estimate_call(sample: Sample, content) -> int:
    return content_tokens(content) + sum(estimate_tokens(p["text"]) for p in sample.prior) + 500


def run_probes(samples: list[Sample], transforms: list, client, ledger: Ledger, model: str = "claude-haiku-5-5",
               log=None) -> list[dict]:
    p_in, p_out, p_w, p_r = _price(model)
    plan = []  # (sample, transform, content)
    est_tokens = 0
    for s in samples:
        variants = [(t, transformed_content(s, t)) for t in transforms]
        variants = [(t, c) for t, c in variants if c is not None]
        if not variants:
            continue
        plan.append((s, variants))
        est_tokens += len(s.probes) * (_estimate_call(s, s.text) + sum(_estimate_call(s, c) for _, c in variants))
    n_calls = sum(len(s.probes) * (1 + len(v)) for s, v in plan)
    estimate = est_tokens * p_in / 1e6 + n_calls * 40 * p_out / 1e6
    ledger.guard(estimate)

    rows: list[dict] = []
    spent = 0.0

    def ask(sample, content, question):
        nonlocal spent
        text, usage = client(build_body(sample, content, question, model))
        spent += ((usage.get("input_tokens", 0) * p_in + usage.get("output_tokens", 0) * p_out
                   + usage.get("cache_creation_input_tokens", 0) * p_w
                   + usage.get("cache_read_input_tokens", 0) * p_r) / 1e6)
        return text

    try:
        for s, variants in plan:
            for p in s.probes:
                orig = ask(s, s.text, p.question)
                orig_ok = is_correct(orig, p.answer, p.kind)
                for t, content in variants:
                    tx = ask(s, content, p.question)
                    row = {"sample": s.id, "family": s.family, "transform": t.name, "probe": p.id,
                           "probe_type": p.kind, "focus": p.focus, "question": p.question, "expected": p.answer,
                           "orig_answer": orig, "tx_answer": tx, "orig_ok": orig_ok,
                           "tx_ok": is_correct(tx, p.answer, p.kind),
                           "orig_tokens": content_tokens(s.text), "tx_tokens": content_tokens(content)}
                    rows.append(row)
                    if log:
                        log(row)
    finally:
        ledger.record("probe", f"fidelity probes: {len(samples)} samples, {len(transforms)} transforms, {model}",
                      spent)
    return rows


class AnthropicMessagesClient:
    """Minimal /v1/messages client (API key from the environment, never logged)."""

    URL = "https://api.anthropic.com/v1/messages"

    def __init__(self, max_retries: int = 6):
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set")
        self.__key = key
        self.max_retries = max_retries

    def __repr__(self) -> str:
        return "AnthropicMessagesClient(key=<redacted>)"

    def __call__(self, body: dict):
        data = json.dumps(body).encode()
        for attempt in range(self.max_retries):
            req = urllib.request.Request(self.URL, data=data, method="POST", headers={
                "x-api-key": self.__key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=120) as resp:
                    out = json.loads(resp.read())
                text = "".join(c.get("text", "") for c in out.get("content", []) if c.get("type") == "text")
                return text.strip(), out.get("usage", {})
            except urllib.error.HTTPError as e:
                if e.code in (429, 500, 502, 503, 529) and attempt < self.max_retries - 1:
                    time.sleep(min(30, 2 ** attempt))
                    continue
                raise RuntimeError(f"messages failed: HTTP {e.code}: {e.read().decode(errors='replace')[:300]}") from None
        raise RuntimeError("messages: retries exhausted")
