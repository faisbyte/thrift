"""Reconstruct historical /v1/messages request bodies from Claude Code transcripts.

Claude Code transcripts (~/.claude/projects/<project>/<session>.jsonl) record,
per API request:
  - ``api-request-shape``: the system blocks and tool definitions (by shapeHash)
  - ``api-request-blob``: message bodies, keyed by the sha256 of their
    canonical JSON (sorted keys, compact separators)
  - ``api-request``: params, ``keep`` (messages carried over from the previous
    request) + ``tail`` (hashes of the new messages), and cache_control paths.
Assistant replies are not stored as blobs; they are rebuilt by merging the
per-block ``assistant`` records that share a message id. User tool results
come from ``user`` records (merged when consecutive).
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


def hash_message(msg: dict) -> str:
    return hashlib.sha256(json.dumps(msg, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass
class RecordedRequest:
    session_id: str
    index: int
    request_id: str
    ts: float
    model: str
    query_source: str
    body: dict
    complete: bool
    missing: int = 0
    approx: bool = False  # some messages were approximated from transcript records (not byte-exact)
    source: str = ""
    meta: dict = field(default_factory=dict)


def _epoch(ts: str | None) -> float:
    if not ts:
        return 0.0
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()


def _set_path(root, path, value) -> bool:
    node = root
    try:
        for key in path[:-1]:
            node = node[key]
        target = node[path[-1]]
    except (KeyError, IndexError, TypeError):
        return False
    if isinstance(target, dict):
        target["cache_control"] = value
        return True
    return False


def _candidates(records: list[dict]) -> dict[str, dict]:
    cands: dict[str, dict] = {}
    for r in records:
        if r.get("type") == "api-request-blob" and "hash" in r:
            cands[r["hash"]] = r["message"]
    groups: dict[str, list] = {}
    order: list[str] = []
    run: list[dict] = []

    def flush_users():
        if not run:
            return
        content = []
        for m in run:
            c = m.get("content")
            content.extend(c if isinstance(c, list) else [{"type": "text", "text": c or ""}])
        for msg in ({"role": "user", "content": content},
                    *({"role": "user", "content": m.get("content")} for m in run)):
            cands.setdefault(hash_message(msg), msg)
        run.clear()

    for r in records:
        t = r.get("type")
        if t == "assistant":
            flush_users()
            m = r.get("message") or {}
            mid = m.get("id") or r.get("uuid")
            if mid not in groups:
                groups[mid] = []
                order.append(mid)
            groups[mid].extend(m.get("content") or [])
        elif t == "user":
            m = r.get("message") or {}
            if m.get("role") == "user":
                run.append(m)
    flush_users()
    for mid in order:
        msg = {"role": "assistant", "content": groups[mid]}
        cands.setdefault(hash_message(msg), msg)
    return cands


def _tool_results(records: list[dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for r in records:
        if r.get("type") != "user":
            continue
        c = (r.get("message") or {}).get("content")
        for b in c if isinstance(c, list) else []:
            if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("tool_use_id"):
                out.setdefault(b["tool_use_id"], b)
    return out


def _approximate(prev_msg: dict | None, results: dict[str, dict]) -> dict | None:
    """Rebuild a tool-result user message from the tool_use ids of the assistant turn before it."""
    if not prev_msg or prev_msg.get("role") != "assistant":
        return None
    ids = [b.get("id") for b in prev_msg.get("content") or [] if isinstance(b, dict) and b.get("type") == "tool_use"]
    if not ids or any(i not in results for i in ids):
        return None
    return {"role": "user", "content": [copy.deepcopy(results[i]) for i in ids]}


def reconstruct_session(records: list[dict], source: str = "") -> list[RecordedRequest]:
    shapes = {r["shapeHash"]: r["shape"] for r in records if r.get("type") == "api-request-shape"}
    cands = _candidates(records)
    results = _tool_results(records)
    out: list[RecordedRequest] = []
    prev: list = []  # previous request's messages (None = unresolved)
    prev_flags: list = []  # True where a message was approximated
    for i, r in enumerate(x for x in records if x.get("type") == "api-request"):
        keep = int(r.get("keep") or 0)
        msgs = prev[:keep]
        flags = prev_flags[:keep]
        for h in r.get("tail") or []:
            m = copy.deepcopy(cands.get(h))
            approx_here = False
            if m is None:
                m = _approximate(msgs[-1] if msgs else None, results)
                approx_here = m is not None
            msgs.append(m)
            flags.append(approx_here)
        missing = sum(1 for m in msgs if m is None)
        shape = shapes.get(r.get("shapeHash"))
        params = r.get("params") or {}
        body: dict = {"model": params.get("model", ""), "max_tokens": params.get("max_tokens", 4096)}
        if params.get("thinking"):
            body["thinking"] = params["thinking"]
        if shape is not None:
            body["system"] = copy.deepcopy(shape.get("system"))
            body["tools"] = copy.deepcopy(shape.get("tools") or [])
        # strip cache markers carried from earlier requests before applying this request's markers
        clean = []
        for m in msgs:
            if m is None:
                clean.append(None)
                continue
            m = copy.deepcopy(m)
            if isinstance(m.get("content"), list):
                for b in m["content"]:
                    if isinstance(b, dict):
                        b.pop("cache_control", None)
            clean.append(m)
        body["messages"] = [m if m is not None else {"role": "user", "content": "<unresolved>"} for m in clean]
        for cc in r.get("cacheControl") or []:
            _set_path(body, cc.get("path") or [], cc.get("value"))
        out.append(RecordedRequest(
            session_id=r.get("sessionId", ""), index=i, request_id=r.get("id", ""), ts=_epoch(r.get("timestamp")),
            model=params.get("model", ""), query_source=r.get("querySource", ""), body=body,
            complete=missing == 0 and shape is not None and not any(flags), missing=missing, source=source,
            approx=missing == 0 and shape is not None and any(flags),
            meta={"betas": params.get("betas", []), "effort": (params.get("output_config") or {}).get("effort")},
        ))
        prev = clean
        prev_flags = flags
    return out


def load_jsonl_session(path: str | os.PathLike) -> list[dict]:
    recs = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                recs.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return recs


def iter_transcript_sessions(root: str | os.PathLike = "~/.claude/projects"):
    """Yield (path, [RecordedRequest]) for every transcript under ``root``."""
    base = Path(os.path.expanduser(str(root)))
    for p in sorted(base.glob("*/*.jsonl")):
        reqs = reconstruct_session(load_jsonl_session(p), source=str(p))
        if reqs:
            yield p, reqs


def conversation_from_records(records: list[dict]) -> dict:
    """Approximate the whole main-chain conversation of a session from its
    user/assistant transcript records (sidechain/subagent records skipped).

    Not byte-exact; used for block-level statistics over every session,
    including older transcripts that have no api-request records.
    """
    messages: list[dict] = []
    current_id = None
    for r in records:
        t = r.get("type")
        if t not in ("user", "assistant") or r.get("isSidechain"):
            continue
        m = r.get("message") or {}
        c = m.get("content")
        blocks = c if isinstance(c, list) else [{"type": "text", "text": c or ""}]
        if t == "assistant":
            mid = m.get("id")
            if messages and messages[-1]["role"] == "assistant" and mid == current_id:
                messages[-1]["content"].extend(copy.deepcopy(blocks))
            else:
                messages.append({"role": "assistant", "content": copy.deepcopy(blocks)})
            current_id = mid
        else:
            if messages and messages[-1]["role"] == "user":
                messages[-1]["content"].extend(copy.deepcopy(blocks))
            else:
                messages.append({"role": "user", "content": copy.deepcopy(blocks)})
            current_id = None
    conv: dict = {"messages": messages}
    shape = next((r["shape"] for r in records if r.get("type") == "api-request-shape"), None)
    if shape:
        conv["system"] = copy.deepcopy(shape.get("system"))
        conv["tools"] = copy.deepcopy(shape.get("tools") or [])
    return conv
