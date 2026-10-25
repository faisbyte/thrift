"""Which transforms are enabled. Defaults come from the scoreboard
(router/defaults.json); REPROUTER_TRANSFORMS (comma list) overrides."""

from __future__ import annotations

import json
import os
from pathlib import Path

from .router import Policy

DEFAULTS_FILE = Path(__file__).resolve().parents[2] / "defaults.json"


def enabled_names() -> list[str]:
    env = os.environ.get("REPROUTER_TRANSFORMS")
    if env is not None:
        return [x.strip() for x in env.split(",") if x.strip()]
    try:
        return list(json.loads(DEFAULTS_FILE.read_text()).get("enabled", []))
    except (OSError, ValueError):
        return []


def build_policy(names: list[str] | None = None, **kwargs) -> Policy:
    from . import transforms  # noqa: F401  (registers every transform module)
    from .transforms.base import registry

    reg = registry()
    chosen = [reg[n] for n in (names if names is not None else enabled_names()) if n in reg]
    return Policy(transforms=chosen, **kwargs)
