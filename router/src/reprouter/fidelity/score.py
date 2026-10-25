"""Answer matching and paired statistics for fidelity probes."""

from __future__ import annotations

import math
import re
from collections import defaultdict

_NUM = re.compile(r"-?\d+(?:\.\d+)?")


def normalize(s: str) -> str:
    s = (s or "").strip().strip("`'\" ").rstrip(".,;:!").strip("`'\" ")
    return s.lower()


def is_correct(answer: str, expected: str, kind: str) -> bool:
    a = normalize(answer)
    e = normalize(expected)
    if kind == "absent":
        return "not present" in a
    if kind == "number":
        nums = _NUM.findall(a.replace(",", ""))
        if not nums:
            return False
        try:
            want = float(e.replace(",", ""))
        except ValueError:
            return False
        return abs(float(nums[-1]) - want) <= 1e-6 * max(1.0, abs(want))
    # exact
    if a == e:
        return True
    if " or " in f" {a} " or len(a.split()) > 8:
        return False
    return re.search(r"(?<![\w./-])" + re.escape(e) + r"(?![\w/-])", a) is not None


def mcnemar_p(lost: int, gained: int) -> float:
    """Exact two-sided McNemar (binomial) p-value for discordant pairs."""
    n = lost + gained
    if n == 0:
        return 1.0
    k = min(lost, gained)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n
    return min(1.0, 2 * tail)


def _stats(rows: list[dict]) -> dict:
    n = len(rows)
    paired = [r for r in rows if r["orig_ok"]]
    lost = sum(1 for r in paired if not r["tx_ok"])
    gained = sum(1 for r in rows if not r["orig_ok"] and r["tx_ok"])
    return {
        "n": n,
        "n_paired": len(paired),
        "orig_acc": sum(r["orig_ok"] for r in rows) / n if n else 0.0,
        "tx_acc": sum(r["tx_ok"] for r in rows) / n if n else 0.0,
        "retained": sum(r["tx_ok"] for r in paired) / len(paired) if paired else 0.0,
        "lost": lost,
        "gained": gained,
        "p_value": mcnemar_p(lost, gained),
    }


# What each lossy transform promises to keep; its verdict is judged on these probes.
PROMISES = {"log_filter": {"errors"}, "skeleton": {"signatures"}}


def summarize(rows: list[dict], promises: dict | None = None) -> dict:
    promises = PROMISES if promises is None else promises
    by_t: dict[str, list] = defaultdict(list)
    for r in rows:
        by_t[r["transform"]].append(r)
    out = {}
    for t, rs in by_t.items():
        s = _stats(rs)
        types: dict[str, list] = defaultdict(list)
        focus: dict[str, list] = defaultdict(list)
        for r in rs:
            types[r["probe_type"]].append(r)
            focus[r.get("focus", "general")].append(r)
        s["by_type"] = {k: _stats(v) for k, v in types.items()}
        s["by_focus"] = {k: _stats(v) for k, v in focus.items()}
        keep = promises.get(t)
        s["promised"] = _stats([r for r in rs if keep is None or r.get("focus", "general") in keep])
        out[t] = s
    return out
