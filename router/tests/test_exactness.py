from reprouter.exactness import exact_risks
from reprouter.request import Block


def tr(text, tool="Bash", tool_input=None):
    return Block(("messages", 2, "content", 0), "user", "tool_result", text, msg_index=2,
                 tool_name=tool, tool_input=tool_input or {})


def test_git_and_sha256_hashes():
    assert "hash" in exact_risks(tr("commit 9d8f9a372cc5916fd4781fec207ced7ddac2f08f"))
    assert "hash" in exact_risks(tr("abc1234 fix bug"))  # short git sha
    assert "hash" not in exact_risks(tr("total 1234567 bytes"))  # digits only is not a hash
    assert "hash" not in exact_risks(tr("deadline accepted"))  # letters only is not a hash


def test_uuid():
    assert "uuid" in exact_risks(tr("session 93b4bea6-345f-4c05-a1fc-65d2b3eef08e started"))


def test_paths():
    assert "path" in exact_risks(tr("see /work/proj/rich/console.py line 3"))
    assert "path" in exact_risks(tr("modified: src/app/main.ts"))
    assert "path" not in exact_risks(tr("and/or maybe"))


def test_ids():
    assert "id" in exact_risks(tr("request req_018EeWyXxfu5pfWkrYcMdjWG failed"))
    assert "id" in exact_risks(tr("see JIRA-1234 and PROJ-77"))


def test_code_edit_target_only_inside_project():
    inside = tr("1\tx = 1\n", "Read", {"file_path": "/work/proj/m.py"})
    outside = tr("1\tx = 1\n", "Read", {"file_path": "/usr/lib/python3.12/json/__init__.py"})
    assert "code_edit_target" in exact_risks(inside, project_root="/work/proj")
    assert "code_edit_target" not in exact_risks(outside, project_root="/work/proj")


def test_unknown_project_root_treats_every_read_as_edit_target():
    assert "code_edit_target" in exact_risks(tr("1\tx\n", "Read", {"file_path": "/anything.py"}), project_root=None)


def test_plain_prose_has_no_risks():
    assert exact_risks(tr("All good, nothing to report today.")) == set()
