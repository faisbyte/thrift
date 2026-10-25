"""Spend ledger with a hard cap on live API spend.

Every paid call must ``guard(estimate)`` before running and ``record(...)``
after. Subscription usage (``billed=False``) is reported but does not count
toward the cap, which applies to real API spend only.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

_SECRET = re.compile(r"sk-ant-[A-Za-z0-9_-]{8,}")


class BudgetExceeded(RuntimeError):
    pass


class Ledger:
    def __init__(self, path: str | os.PathLike, cap_usd: float = 3.0):
        self.path = Path(path)
        self.cap_usd = cap_usd

    def _entries(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(x) for x in self.path.read_text().splitlines() if x.strip()]

    def total(self) -> float:
        return sum(e["cost_usd"] for e in self._entries() if e.get("billed", True))

    def unbilled_total(self) -> float:
        return sum(e["cost_usd"] for e in self._entries() if not e.get("billed", True))

    def remaining(self) -> float:
        return max(0.0, self.cap_usd - self.total())

    def guard(self, estimate_usd: float) -> None:
        spent = self.total()
        if spent + estimate_usd > self.cap_usd + 1e-9:
            raise BudgetExceeded(
                f"spend cap ${self.cap_usd:.2f} would be exceeded: spent ${spent:.2f}, "
                f"this call estimated ${estimate_usd:.2f}"
            )

    def record(self, kind: str, desc: str, cost_usd: float, billed: bool = True) -> None:
        if _SECRET.search(desc):
            raise ValueError("refusing to write something that looks like a secret to the ledger")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        entry = {"ts": round(time.time(), 3), "kind": kind, "desc": desc,
                 "cost_usd": round(float(cost_usd), 6), "billed": billed}
        with open(self.path, "a") as f:
            f.write(json.dumps(entry) + "\n")
