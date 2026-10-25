"""Offline per-transform scoreboard: savings (exact token counts, cache-weighted)
vs fidelity (probe retention), and a keep/disable verdict per transform.

Cost model, in input-token equivalents per conversation:
  - fixed prefix (system + tools): written once, read on every later turn
  - each block: written once when it enters, read on every later turn
    (turns_after = assistant turns after the block's message)
So a saved token in a block is worth (write + read x turns_after). Output
tokens and agent-behaviour effects (extra turns, re-reads) are NOT modeled;
the end-to-end benchmark measures those.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field

from .classify import classify_request
from .router import Policy, decide
from .tokens import image_tokens
from .transforms.base import Context


@dataclass(frozen=True)
class Regime:
    write: float
    read: float
    label: str


REGIMES = {
    "api_5m_haiku": Regime(1.25, 0.10, "API key, 5m cache, Haiku/other read 0.1x"),
    "sub_1h_sonnet": Regime(2.0, 0.05, "subscription, 1h cache, Sonnet/Opus 5.5 read 0.05x"),
}


@dataclass
class ToolResultRecord:
    session: str
    block: object
    label: object
    prior: list
    turns_after: int
    project_root: str | None


@dataclass
class TransformStats:
    name: str
    lossless: bool
    applied: int = 0
    tokens_before: int = 0
    tokens_after: int = 0
    weighted: dict = field(default_factory=lambda: {k: 0.0 for k in REGIMES})
    by_kind: dict = field(default_factory=lambda: defaultdict(lambda: {"applied": 0, "before": 0, "after": 0,
                                                                      "saved": 0}))


@dataclass
class Scoreboard:
    transforms: dict
    tool_result_tokens: int = 0
    session_weighted: dict = field(default_factory=lambda: {k: 0.0 for k in REGIMES})
    sessions: int = 0
    tool_results: int = 0


def _assistant_indices(body: dict) -> list[int]:
    return [i for i, m in enumerate(body.get("messages") or []) if m.get("role") == "assistant"]


def _block_text(block) -> str:
    if block.type == "tool_use":
        return json.dumps(block.tool_input or {}, sort_keys=True)
    return block.text or ""


def iter_tool_results(conversations: list[dict]):
    for conv in conversations:
        body = conv["body"]
        assistants = _assistant_indices(body)
        from .classify import project_root

        root = project_root(body)
        prior = []
        for block, label in classify_request(body):
            if block.type == "tool_result":
                after = sum(1 for a in assistants if a > (block.msg_index or 0))
                yield ToolResultRecord(conv.get("session", ""), block, label, list(prior), after, root)
            prior.append((block, label))


def _content_tokens(content, count) -> int:
    if isinstance(content, str):
        return count(content)
    total = 0
    for c in content or []:
        if c.get("type") == "text":
            total += count(c.get("text", ""))
        elif c.get("type") == "image":
            from .transforms.image import png_size

            total += image_tokens(*png_size(c["source"]["data"]))
    return total


def measure(conversations: list[dict], transforms: list, count, prefix_tokens: int = 0,
            allow_nontext: bool = True) -> Scoreboard:
    sb = Scoreboard(transforms={t.name: TransformStats(t.name, t.lossless) for t in transforms})
    for conv in conversations:
        body = conv["body"]
        assistants = _assistant_indices(body)
        n_turns = max(len(assistants), 1)
        sb.sessions += 1
        for k, r in REGIMES.items():
            sb.session_weighted[k] += prefix_tokens * (r.write + r.read * (n_turns - 1))
        for block, label in classify_request(body):
            if block.msg_index is None:
                continue
            after = sum(1 for a in assistants if a > block.msg_index)
            n = count(_block_text(block))
            for k, r in REGIMES.items():
                sb.session_weighted[k] += n * (r.write + r.read * after)

    for rec in iter_tool_results(conversations):
        sb.tool_results += 1
        before = count(rec.block.text or "")
        sb.tool_result_tokens += before
        ctx = Context(project_root=rec.project_root, prior=rec.prior)
        for t in transforms:
            policy = Policy(transforms=[t], margin=0.0, allow_nontext=allow_nontext)
            decision, content = decide(rec.block, rec.label, ctx, policy)
            if content is None:
                continue
            after_n = _content_tokens(content, count)
            if after_n >= before:
                continue
            st = sb.transforms[t.name]
            st.applied += 1
            st.tokens_before += before
            st.tokens_after += after_n
            saved = before - after_n
            for k, r in REGIMES.items():
                st.weighted[k] += saved * (r.write + r.read * rec.turns_after)
            bk = st.by_kind[rec.label.kind]
            bk["applied"] += 1
            bk["before"] += before
            bk["after"] += after_n
            bk["saved"] += saved
    return sb


def verdicts(sb: Scoreboard, fidelity: dict, min_share: float = 0.001, min_retained: float = 0.95,
             lossless_exempt: set | None = None, structural: dict | None = None) -> dict:
    lossless_exempt = lossless_exempt or set()
    structural = structural or {}
    out = {}
    for name, st in sb.transforms.items():
        share = (st.weighted["api_5m_haiku"] / sb.session_weighted["api_5m_haiku"]
                 if sb.session_weighted["api_5m_haiku"] else 0.0)
        if st.applied == 0:
            out[name] = (False, "no savings on the corpus (never applied)")
            continue
        if share < min_share:
            out[name] = (False, f"savings too small ({share:.3%} of session cost < {min_share:.2%})")
            continue
        if name in structural:
            out[name] = (False, structural[name])
            continue
        if name not in lossless_exempt:
            f = fidelity.get(name)
            if not f:
                out[name] = (False, "untested fidelity (no probe results)")
                continue
            if f["retained"] < min_retained:
                out[name] = (False, f"fidelity {f['retained']:.0%} < {min_retained:.0%}")
                continue
        out[name] = (True, f"saves {share:.2%} of session cost")
    return out


def render_markdown(sb: Scoreboard, fidelity: dict, verdict: dict) -> str:
    lines = [f"Corpus: {sb.sessions} sessions, {sb.tool_results} tool results, "
             f"{sb.tool_result_tokens:,} tool-result tokens (count_tokens).", "",
             "| transform | lossless | blocks | tool-result tokens before → after | saved | "
             + " | ".join(f"share of session cost ({k})" for k in REGIMES)
             + " | fidelity retained (paired n) | verdict |",
             "|---|---|---|---|---|" + "---|" * len(REGIMES) + "---|---|"]
    for name, st in sorted(sb.transforms.items(), key=lambda kv: -kv[1].weighted["api_5m_haiku"]):
        saved = st.tokens_before - st.tokens_after
        shares = [f"{st.weighted[k] / sb.session_weighted[k]:.2%}" if sb.session_weighted[k] else "–"
                  for k in REGIMES]
        f = fidelity.get(name)
        fid = f"{f['retained']:.0%} ({f['n_paired']})" if f else "–"
        v = verdict.get(name, (False, ""))
        lines.append(f"| {name} | {'yes' if st.lossless else 'no'} | {st.applied} | "
                     f"{st.tokens_before:,} → {st.tokens_after:,} | {saved:,} | " + " | ".join(shares)
                     + f" | {fid} | {'KEEP' if v[0] else 'off'}: {v[1]} |")
    return "\n".join(lines) + "\n"
