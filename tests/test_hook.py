import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from conftest import write
from test_ranking import CORPUS
from thriftctx import store
from thriftctx.hook_prompt import build_context
from thriftctx.indexer import index_project

PLUGIN = Path(__file__).resolve().parents[1] / "plugins" / "thrift"
HOOK_SCRIPT = PLUGIN / "scripts" / "run-hook.sh"


@pytest.fixture
def project(tmp_path):
    for rel, text in CORPUS.items():
        write(tmp_path, rel, text.lstrip("\n"))
    index_project(tmp_path)
    return tmp_path


def run_hook(payload: dict, env_extra: dict | None = None) -> tuple[str, int, float]:
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "VIRTUAL_ENV")}
    env.update({"CLAUDE_PLUGIN_ROOT": str(PLUGIN), "CLAUDE_PLUGIN_DATA": "/nonexistent-thrift-data"})
    env.update(env_extra or {})
    t = time.perf_counter()
    p = subprocess.run(["bash", str(HOOK_SCRIPT)], input=json.dumps(payload), capture_output=True,
                       text=True, env=env, timeout=10)
    return p.stdout, p.returncode, time.perf_counter() - t


def test_build_context_points_at_code(project):
    ctx = build_context({"prompt": "the session refresh_token logic is broken", "cwd": str(project)})
    assert ctx.startswith("thrift index matches")
    assert "src/auth/session.py" in ctx and "SessionStore.refresh_token" in ctx
    assert len(ctx) <= 600  # ~150 tokens


def test_silent_cases(project):
    cwd = str(project)
    for prompt in ["", "hi", "/compact", "thanks, looks good", "ok now run the tests again"]:
        assert build_context({"prompt": prompt, "cwd": cwd}) == "", prompt


def test_pasted_content_is_ignored(project):
    prompt = ("<pasted_content id=\"x\">\nrefresh_token SessionStore login_handler\n</pasted_content id=\"x\">\n"
              "thoughts?")
    assert build_context({"prompt": prompt, "cwd": str(project)}) == ""


def test_no_index_is_silent(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    assert build_context({"prompt": "refresh_token is broken", "cwd": str(tmp_path)}) == ""


def test_deadline_exceeded_is_silent(project):
    ctx = build_context({"prompt": "refresh_token is broken", "cwd": str(project)}, t0=time.monotonic() - 10)
    assert ctx == ""


def test_hook_script_end_to_end(project):
    out, code, secs = run_hook({"prompt": "fix exponential_backoff for retries", "cwd": str(project)},
                               {"CLAUDE_PROJECT_DIR": str(project)})
    assert code == 0
    data = json.loads(out)
    ctx = data["hookSpecificOutput"]["additionalContext"]
    assert data["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert "src/http/retry.py" in ctx
    assert secs < 1.0  # generous for CI; the in-process budget is 250 ms


def test_hook_script_latency_budget(project):
    # warm-up then measure: process start + query should be well under 300 ms
    run_hook({"prompt": "x", "cwd": str(project)})
    _, _, secs = run_hook({"prompt": "SessionStore refresh_token", "cwd": str(project)},
                          {"CLAUDE_PROJECT_DIR": str(project)})
    assert secs < 0.3


def test_hook_script_never_fails(tmp_path, project):
    for stdin_payload, env in [
        ({"prompt": "refresh_token", "cwd": "/"}, {"CLAUDE_PROJECT_DIR": "/nonexistent/thrift-test"}),
        ({"prompt": "refresh_token", "cwd": str(project)}, {"THRIFT_HOOK": "0"}),
    ]:
        out, code, _ = run_hook(stdin_payload, env)
        assert code == 0 and out == ""
    # corrupt database
    store.db_path(project).write_bytes(b"not a database at all")
    out, code, _ = run_hook({"prompt": "refresh_token SessionStore", "cwd": str(project)},
                            {"CLAUDE_PROJECT_DIR": str(project)})
    assert code == 0 and out == ""


def test_hook_log(project, tmp_path):
    log = tmp_path / "hook.jsonl"
    run_hook({"prompt": "SessionStore refresh_token", "cwd": str(project)},
             {"CLAUDE_PROJECT_DIR": str(project), "THRIFT_HOOK_LOG": str(log)})
    entry = json.loads(log.read_text().splitlines()[-1])
    assert entry["fired"] and entry["error"] is None and entry["chars"] > 0


def test_hook_survives_locked_db(project):
    conn = sqlite3.connect(store.db_path(project))
    conn.execute("BEGIN EXCLUSIVE")  # writer holding the DB (WAL: readers still work)
    try:
        ctx = build_context({"prompt": "SessionStore refresh_token", "cwd": str(project)})
        assert ctx == "" or "session.py" in ctx
    finally:
        conn.rollback()
        conn.close()
