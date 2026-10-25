import json

import pytest

from rr_fixtures import claude_code_request
from reprouter.classify import classify_request, project_root
from reprouter.request import Block


def tr(text, tool="Bash", tool_input=None, is_error=False):
    return Block(("messages", 2, "content", 0), "user", "tool_result", text, msg_index=2,
                 tool_name=tool, tool_input=tool_input or {}, is_error=is_error)


def kind_of(block, root="/work/proj"):
    from reprouter.classify import classify_block

    return classify_block(block, project_root=root).kind


def test_project_root_from_claude_code_system_prompt():
    assert project_root(claude_code_request()) == "/work/proj"
    req = claude_code_request()
    req["system"][2]["text"] = "Primary working directory: /Users/me/code/app\nIs a git repository: true"
    assert project_root(req) == "/Users/me/code/app"


def test_static_blocks():
    labels = {b.path: c for b, c in classify_request(claude_code_request())}
    assert labels[("system", 1)].kind == "system_prompt"
    assert labels[("tools", 0)].kind == "tool_definitions"
    assert labels[("messages", 0, "content", 0)].kind == "user_prompt"
    assert labels[("messages", 1, "content", 0)].kind == "thinking"
    assert labels[("messages", 1, "content", 1)].kind == "tool_use"


def test_recency_entry_recent_old():
    req = claude_code_request()
    labels = {b.path: c for b, c in classify_request(req, recent_messages=2)}
    assert labels[("messages", 4, "content", 0)].recency == "entry"  # after the last assistant turn
    assert labels[("messages", 3, "content", 0)].recency == "recent"
    assert labels[("messages", 0, "content", 0)].recency == "old"
    assert labels[("system", 1)].recency == "static"


def test_system_reminder_user_text():
    b = Block(("messages", 0, "content", 1), "user", "text", "<system-reminder>\nfile changed\n</system-reminder>", msg_index=0)
    assert kind_of(b) == "system_reminder"


@pytest.mark.parametrize("text,tool,expected", [
    ("1\tdef f():\n2\t    return 1\n", "Read", "tool_result:file_read"),
    ("The file /work/proj/m.py has been updated successfully.", "Edit", "tool_result:edit_confirmation"),
    ("Found 3 files\nrich/a.py\nrich/b.py\nrich/c.py", "Grep", "tool_result:search_results"),
    (json.dumps([{"id": 1, "name": "a"}, {"id": 2, "name": "b"}], indent=2), "Bash", "tool_result:json"),
    ("diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1,2 +1,2 @@\n-a\n+b\n", "Bash", "tool_result:diff"),
    ("============ test session starts ============\ncollected 5 items\n\ntests/test_a.py ..F..\n=== 1 failed, 4 passed in 0.12s ===", "Bash", "tool_result:test_output"),
    ("2026-10-25 10:00:01 INFO start\n2026-10-25 10:00:02 WARN slow\n2026-10-25 10:00:03 ERROR boom\n2026-10-25 10:00:04 INFO done", "Bash", "tool_result:log"),
    ("name,size,owner\na.txt,10,root\nb.txt,20,me\nc.txt,30,you", "Bash", "tool_result:tabular"),
    ("hello from a script", "Bash", "tool_result:command_output"),
])
def test_tool_result_kinds(text, tool, expected):
    assert kind_of(tr(text, tool)) == expected


def test_error_results_are_their_own_kind():
    assert kind_of(tr("Exit code 1\nboom", "Bash", is_error=True)) == "tool_result:error"


def test_numbered_lines_from_unknown_tool_still_file_read():
    assert kind_of(tr("     1\timport os\n     2\timport sys\n     3\t", "mcp__x__read")) == "tool_result:file_read"
