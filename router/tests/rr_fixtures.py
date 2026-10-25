"""Realistic request fixtures shaped like Claude Code /v1/messages bodies."""

import copy

READ_OUT = "1\tdef add(a, b):\n2\t    return a + b\n3\t\n4\tdef sub(a, b):\n5\t    return a - b\n"


def claude_code_request() -> dict:
    return copy.deepcopy({
        "model": "claude-haiku-5-5",
        "max_tokens": 32000,
        "system": [
            {"type": "text", "text": "x-anthropic-billing-header: cc_version=2.1.310"},
            {"type": "text", "text": "You are Claude Code.", "cache_control": {"type": "ephemeral", "ttl": "1h"}},
            {"type": "text", "text": "# Environment\ncwd: /work/proj", "cache_control": {"type": "ephemeral"}},
        ],
        "tools": [
            {"name": "Read", "description": "Reads a file", "input_schema": {"type": "object"}},
            {"name": "Bash", "description": "Runs a command", "input_schema": {"type": "object"}},
        ],
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "Fix the sub function in /work/proj/m.py"}]},
            {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "", "signature": "sig1"},
                {"type": "tool_use", "id": "tu_1", "name": "Read", "input": {"file_path": "/work/proj/m.py"}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "tu_1", "content": READ_OUT},
            ]},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "tu_2", "name": "Bash", "input": {"command": "pytest -q"}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "tu_2",
                 "content": [{"type": "text", "text": "....F\nFAILED test_m.py::test_sub\n1 failed, 4 passed"}],
                 "cache_control": {"type": "ephemeral", "ttl": "1h"}},
            ]},
        ],
    })
