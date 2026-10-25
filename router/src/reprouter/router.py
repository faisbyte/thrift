"""Per-block representation choice.

For each tool_result block: run cleanup-stage transforms in order, then pick
the cheapest main-stage candidate, subject to fidelity rules; keep it only if
it saves at least ``margin`` of the block's tokens.

Every decision is a pure function of the block, its label (kind and exactness
risks, never recency) and the blocks before it. That makes routed history
byte-identical from turn to turn, which prompt caching and preserved-thinking
binding both require. Only blocks at or after ``min_msg_index`` are eligible,
so history that predates the router stays untouched.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field

from .classify import classify_request, project_root
from .request import apply_replacements
from .tokens import estimate_tokens, image_tokens
from .transforms.base import Context, Result, Transform

LOSSY_BLOCKED = frozenset({"code_edit_target"})
NONTEXT_BLOCKED = frozenset({"code_edit_target", "hash", "uuid", "id"})


@dataclass
class Policy:
    transforms: list = field(default_factory=list)
    margin: float = 0.10
    allow_nontext: bool = False
    lossy_blocked: frozenset = LOSSY_BLOCKED
    nontext_blocked: frozenset = NONTEXT_BLOCKED


@dataclass
class Decision:
    path: tuple
    kind: str
    transform: str | None
    tokens_before: int
    tokens_after: int
    lossless: bool = True
    reason: str = ""


def content_tokens(content) -> int:
    if isinstance(content, str):
        return estimate_tokens(content)
    total = 0
    for c in content or []:
        if c.get("type") == "text":
            total += estimate_tokens(c.get("text", ""))
        elif c.get("type") == "image":
            src = c.get("source") or {}
            try:
                from .transforms.image import png_size

                total += image_tokens(*png_size(src.get("data", "")))
            except Exception:
                total += 1600
    return total


def _blocked(t: Transform, res: Result, risks: set, policy: Policy) -> str | None:
    if not res.text_only:
        if not policy.allow_nontext:
            return "non-text representations disabled"
        if risks & policy.nontext_blocked:
            return "exact-sensitive: non-text representation skipped"
    if (not t.lossless or not res.lossless) and risks & policy.lossy_blocked:
        return "exact-sensitive: lossy transform skipped"
    return None


def route(req: dict, policy: Policy, min_msg_index: int = 0) -> tuple[dict, list[Decision]]:
    labeled = classify_request(req)
    root = project_root(req)
    model = req.get("model", "")
    cleanup = [t for t in policy.transforms if getattr(t, "stage", "main") == "cleanup"]
    main = [t for t in policy.transforms if getattr(t, "stage", "main") != "cleanup"]
    prior: list = []
    replacements: dict = {}
    decisions: list[Decision] = []

    for block, label in labeled:
        if block.type != "tool_result" or (block.msg_index or 0) < min_msg_index:
            prior.append((block, label))
            continue
        ctx = Context(project_root=root, prior=list(prior), model=model)
        before = estimate_tokens(block.text)
        cur = block
        applied: list[str] = []
        lossless = True
        reasons: list[str] = []

        for t in cleanup:
            res = t.run(cur, label, ctx)
            if res is None or not res.text_only:
                continue
            why = _blocked(t, res, label.risks, policy)
            if why:
                reasons.append(why)
                continue
            cur = dataclasses.replace(cur, text=res.content)
            applied.append(t.name)
            lossless = lossless and t.lossless and res.lossless

        best: tuple[int, Transform, Result] | None = None
        for t in main:
            res = t.run(cur, label, ctx)
            if res is None:
                continue
            why = _blocked(t, res, label.risks, policy)
            if why:
                reasons.append(why)
                continue
            n = content_tokens(res.content)
            if best is None or n < best[0]:
                best = (n, t, res)

        if best is not None:
            final_content, after = best[2].content, best[0]
            applied.append(best[1].name)
            lossless = lossless and best[1].lossless and best[2].lossless
        elif applied:
            final_content, after = cur.text, estimate_tokens(cur.text)
        else:
            decisions.append(Decision(block.path, label.kind, None, before, before,
                                      reason="; ".join(reasons) or "no applicable transform"))
            prior.append((block, label))
            continue

        if after > before * (1 - policy.margin):
            saved = 1 - after / before if before else 0.0
            decisions.append(Decision(block.path, label.kind, None, before, before,
                                      reason=f"below margin ({saved:.0%} < {policy.margin:.0%}) for "
                                             + "+".join(applied)))
        else:
            replacements[block.path] = final_content
            decisions.append(Decision(block.path, label.kind, "+".join(applied), before, after, lossless))
        prior.append((block, label))

    return apply_replacements(req, replacements), decisions
