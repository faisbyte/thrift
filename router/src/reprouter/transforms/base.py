"""Transform interface and registry.

A transform is a pure, deterministic function of (block, label, context):
the same inputs must always produce byte-identical output, because Claude
Code resends history every turn and the router must replay earlier
decisions exactly (prompt caching + preserved-thinking binding).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from ..classify import Label
from ..request import Block

log = logging.getLogger(__name__)


@dataclass
class Context:
    project_root: str | None = None
    prior: list = field(default_factory=list)  # [(Block, Label)] that precede this block in the request
    model: str = ""


@dataclass
class Result:
    content: str | list
    lossless: bool
    note: str = ""

    @property
    def text_only(self) -> bool:
        return isinstance(self.content, (str, dict))  # dict = a rewritten tool_use input

    @property
    def text(self) -> str:
        if isinstance(self.content, str):
            return self.content
        if isinstance(self.content, dict):
            return json.dumps(self.content, ensure_ascii=False)
        return "\n".join(c.get("text", "") for c in self.content if c.get("type") == "text")


class Transform:
    name: str = ""
    lossless: bool = True

    def applicable(self, block: Block, label: Label, ctx: Context) -> bool:  # pragma: no cover - interface
        raise NotImplementedError

    def apply(self, block: Block, label: Label, ctx: Context) -> Result | None:  # pragma: no cover
        raise NotImplementedError

    def run(self, block: Block, label: Label, ctx: Context) -> Result | None:
        """applicable + apply, returning None when declined, unchanged, or on error."""
        try:
            if not self.applicable(block, label, ctx):
                return None
            res = self.apply(block, label, ctx)
        except Exception:  # transforms must never break a request
            log.exception("transform %s failed", self.name)
            return None
        if res is None or (res.text_only and res.text == block.text):
            return None
        return res


_REGISTRY: dict[str, Transform] = {}


def register(t: Transform) -> Transform:
    if t.name in _REGISTRY:
        raise ValueError(f"transform {t.name!r} already registered")
    _REGISTRY[t.name] = t
    return t


def get(name: str) -> Transform:
    return _REGISTRY[name]


def registry() -> dict[str, Transform]:
    return dict(_REGISTRY)
