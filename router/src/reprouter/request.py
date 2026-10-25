"""Parse an Anthropic /v1/messages request body into addressable blocks, and
apply per-block replacements without touching any other bytes.

A block path addresses a JSON location: ("system", i), ("tools", i),
("messages", m, "content", j), or ("messages", m, "content") when a message's
content is a plain string.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field

Path = tuple


@dataclass
class Block:
    path: Path
    role: str  # "system" | "tools" | "user" | "assistant" | "system_msg"
    type: str  # "text" | "tool_result" | "tool_use" | "thinking" | "image" | "tool_def" | other
    text: str = ""
    msg_index: int | None = None
    tool_name: str | None = None
    tool_input: dict = field(default_factory=dict)
    tool_use_id: str | None = None
    is_error: bool = False
    has_cache_control: bool = False
    raw: dict | str | None = None


def _content_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text")
    return ""


def _text_of(block: dict) -> str:
    t = block.get("type")
    if t == "text":
        return block.get("text", "")
    if t == "tool_result":
        return _content_text(block.get("content"))
    if t == "thinking":
        return block.get("thinking", "")
    return ""


def parse_blocks(req: dict) -> list[Block]:
    blocks: list[Block] = []
    system = req.get("system")
    if isinstance(system, str):
        blocks.append(Block(("system",), "system", "text", system, raw=system))
    elif isinstance(system, list):
        for i, b in enumerate(system):
            blocks.append(Block(("system", i), "system", b.get("type", "text"), _text_of(b),
                                has_cache_control="cache_control" in b, raw=b))
    for i, t in enumerate(req.get("tools") or []):
        desc = t.get("description", "")
        blocks.append(Block(("tools", i), "tools", "tool_def", desc, tool_name=t.get("name"),
                            has_cache_control="cache_control" in t, raw=t))

    tool_uses: dict[str, tuple[str, dict]] = {}
    for m, msg in enumerate(req.get("messages") or []):
        role = msg.get("role", "user")
        role = "system_msg" if role == "system" else role
        content = msg.get("content")
        if isinstance(content, str):
            blocks.append(Block(("messages", m, "content"), role, "text", content, msg_index=m, raw=content))
            continue
        for j, b in enumerate(content or []):
            if not isinstance(b, dict):
                continue
            blk = Block(("messages", m, "content", j), role, b.get("type", "?"), _text_of(b), msg_index=m,
                        has_cache_control="cache_control" in b, raw=b)
            if blk.type == "tool_use":
                tool_uses[b.get("id", "")] = (b.get("name", ""), b.get("input") or {})
                blk.tool_name, blk.tool_input, blk.tool_use_id = b.get("name"), b.get("input") or {}, b.get("id")
            elif blk.type == "tool_result":
                blk.tool_use_id = b.get("tool_use_id")
                blk.tool_name, blk.tool_input = tool_uses.get(blk.tool_use_id or "", (None, {}))
                blk.is_error = bool(b.get("is_error"))
            blocks.append(blk)
    return blocks


def _get_parent(root, path: Path):
    node = root
    for key in path[:-1]:
        node = node[key]
    return node, path[-1]


def apply_replacements(req: dict, replacements: dict) -> dict:
    """Return a deep copy of ``req`` with each addressed block's content replaced.

    For tool_result blocks the ``content`` field is replaced (str or list of
    content blocks); for text blocks the ``text`` field; for string message
    content the string itself. All other fields (cache_control, ids) stay.
    """
    out = copy.deepcopy(req)
    for path, new in replacements.items():
        parent, key = _get_parent(out, path)
        target = parent[key]
        if isinstance(target, str):
            parent[key] = new
        elif target.get("type") == "tool_result":
            target["content"] = new
        else:
            target["text"] = new
    return out
