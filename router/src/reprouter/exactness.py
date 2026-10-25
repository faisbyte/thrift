"""Detect byte-exact material in a block.

These risks gate lossy and non-text representations: content with hashes,
UUIDs, IDs or paths must keep those strings verbatim, and a file read inside
the project may be about to be edited, so it must stay exact.
"""

from __future__ import annotations

import os
import re

from .request import Block

_HASH = re.compile(r"(?<![0-9A-Za-z])(?=[0-9a-fA-F]*\d)(?=[0-9a-fA-F]*[a-fA-F])[0-9a-fA-F]{7,64}(?![0-9A-Za-z])")
_UUID = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
_PATH = re.compile(r"(?:(?<![\w.])/[\w.\-]+(?:/[\w.\-]+)+)|(?:\b[\w.\-]+/[\w.\-/]*\.[A-Za-z0-9]{1,8}\b)")
_ID = re.compile(r"\b[a-z]{2,8}_[A-Za-z0-9]{10,}\b|\b[A-Z][A-Z0-9]{1,9}-\d{2,}\b")

READ_TOOLS = {"Read", "NotebookRead"}


def _inside(path: str, root: str) -> bool:
    try:
        return os.path.commonpath([os.path.abspath(path), os.path.abspath(root)]) == os.path.abspath(root)
    except ValueError:
        return False


def exact_risks(block: Block, project_root: str | None = None) -> set[str]:
    text = block.text or ""
    risks: set[str] = set()
    if _UUID.search(text):
        risks.add("uuid")
    if _HASH.search(_UUID.sub(" ", text)):
        risks.add("hash")
    if _PATH.search(text):
        risks.add("path")
    if _ID.search(text):
        risks.add("id")
    fp = (block.tool_input or {}).get("file_path") or (block.tool_input or {}).get("notebook_path")
    if block.type == "tool_result" and (block.tool_name in READ_TOOLS or fp):
        if fp and (project_root is None or _inside(fp, project_root)):
            risks.add("code_edit_target")
        elif not fp and project_root is None:
            risks.add("code_edit_target")
    return risks
