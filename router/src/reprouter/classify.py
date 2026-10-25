"""Label each block of a request: what it is, how recent it is, and whether
it carries byte-exact material."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from .exactness import exact_risks
from .request import Block, parse_blocks

_ROOT = re.compile(r"(?:Primary working directory|cwd|Working directory):\s*(\S+)")
_NUMBERED = re.compile(r"^\s*\d+\t", re.M)
_TEST = re.compile(
    r"=+ .*(passed|failed|error).* in [\d.]+s|test session starts|^FAILED |^Tests?:\s+\d+|"
    r"--- FAIL|^ok\s+\S+\s+[\d.]+s|test result: (ok|FAILED)|\d+ (passing|failing)\b",
    re.M | re.I,
)
_LOGLINE = re.compile(
    r"^(\[?\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}|\[?\d{2}:\d{2}:\d{2}|.*\b(INFO|WARN|WARNING|ERROR|DEBUG|TRACE|FATAL)\b)",
)
_DIFF = re.compile(r"^(diff --git |--- a/|\+\+\+ b/|@@ -\d+(,\d+)? \+\d+(,\d+)? @@)", re.M)
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
SEARCH_TOOLS = {"Grep", "Glob", "LS"}


@dataclass
class Label:
    kind: str
    recency: str = "static"
    risks: set = field(default_factory=set)


def project_root(req: dict) -> str | None:
    system = req.get("system")
    texts = [system] if isinstance(system, str) else [b.get("text", "") for b in system or []]
    for t in texts:
        m = _ROOT.search(t or "")
        if m:
            return m.group(1).rstrip("/")
    return None


def _looks_json(text: str) -> bool:
    s = text.strip()
    if not s or s[0] not in "[{":
        return False
    try:
        json.loads(s)
        return True
    except ValueError:
        return False


def _looks_table(lines: list[str]) -> bool:
    if len(lines) < 3:
        return False
    for delim in (",", "\t", "|"):
        counts = [ln.count(delim) for ln in lines if ln.strip()]
        if counts and counts[0] >= 1 and all(c == counts[0] for c in counts):
            return True
    return False


def tool_result_kind(block: Block) -> str:
    text = block.text or ""
    lines = text.splitlines()
    if block.is_error:
        return "error"
    if block.tool_name in EDIT_TOOLS:
        return "edit_confirmation"
    if block.tool_name == "Read" or (lines and len(_NUMBERED.findall(text)) >= max(2, int(0.8 * len(lines)))):
        return "file_read"
    if block.tool_name in SEARCH_TOOLS:
        return "search_results"
    if _looks_json(text):
        return "json"
    if _DIFF.search(text) and sum(1 for ln in lines if ln[:1] in "+-@") >= 2:
        return "diff"
    if _TEST.search(text):
        return "test_output"
    nonblank = [ln for ln in lines if ln.strip()]
    if len(nonblank) >= 3 and sum(1 for ln in nonblank if _LOGLINE.match(ln)) >= 0.6 * len(nonblank):
        return "log"
    if _looks_table(nonblank):
        return "tabular"
    return "command_output"


def classify_block(block: Block, project_root: str | None = None) -> Label:
    if block.role == "system":
        kind = "system_prompt"
    elif block.role == "tools":
        kind = "tool_definitions"
    elif block.role == "system_msg":
        kind = "mid_system"
    elif block.type == "tool_result":
        kind = "tool_result:" + tool_result_kind(block)
    elif block.type in ("thinking", "redacted_thinking"):
        kind = "thinking"
    elif block.type == "tool_use":
        kind = "tool_use"
    elif block.type == "image":
        kind = "image"
    elif block.role == "assistant":
        kind = "assistant_text"
    elif block.text.lstrip().startswith("<system-reminder>"):
        kind = "system_reminder"
    else:
        kind = "user_prompt"
    return Label(kind=kind, risks=exact_risks(block, project_root))


def classify_request(req: dict, recent_messages: int = 4) -> list[tuple[Block, Label]]:
    blocks = parse_blocks(req)
    root = project_root(req)
    n = len(req.get("messages") or [])
    last_assistant = max((i for i, m in enumerate(req.get("messages") or []) if m.get("role") == "assistant"),
                         default=-1)
    out = []
    for b in blocks:
        label = classify_block(b, root)
        if b.msg_index is None:
            label.recency = "static"
        elif b.msg_index > last_assistant:
            label.recency = "entry"
        elif b.msg_index >= n - recent_messages:
            label.recency = "recent"
        else:
            label.recency = "old"
        out.append((b, label))
    return out
