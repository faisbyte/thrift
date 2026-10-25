"""Shared helpers for the benchmark: task loading, repo/workdir preparation, checks."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

BENCH_DIR = Path(__file__).resolve().parent
REPO_ROOT = BENCH_DIR.parent
PLUGIN_DIR = REPO_ROOT / "plugins" / "thrift"
CACHE_DIR = BENCH_DIR / ".cache"

sys.path.insert(0, str(PLUGIN_DIR / "src"))


def load_dotenv(path: Path = REPO_ROOT / ".env") -> None:
    """Load KEY=VALUE lines from the repo's .env; variables already set win."""
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.removeprefix("export ").partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if key and value and key not in os.environ:
            os.environ[key] = value


load_dotenv()


@dataclass
class Task:
    id: str
    kind: str
    prompt: str
    split: str = "dev"
    gold_files: list[str] = field(default_factory=list)
    expect_all: list[str] = field(default_factory=list)
    expect_any: list[str] = field(default_factory=list)
    setup: list[dict] = field(default_factory=list)
    check: str | None = None
    no_suffix: bool = False

    def full_prompt(self, defaults: dict) -> str:
        if self.no_suffix:
            return self.prompt
        suffix = defaults.get("qa_suffix" if self.kind == "qa" else "edit_suffix", "")
        return f"{self.prompt.strip()}\n\n{suffix.strip()}".strip()


@dataclass
class Suite:
    repo: dict
    defaults: dict
    tasks: list[Task]

    def select(self, ids: list[str] | None = None, splits: list[str] | None = None) -> list[Task]:
        out = self.tasks
        if ids:
            out = [t for t in out if t.id in ids]
        if splits:
            out = [t for t in out if t.split in splits]
        return out


def load_suite(path: str | os.PathLike) -> Suite:
    data = yaml.safe_load(Path(path).read_text())
    tasks = [Task(**t) for t in data["tasks"]]
    return Suite(repo=data["repo"], defaults=data.get("defaults", {}), tasks=tasks)


def run(cmd: list[str] | str, cwd: Path | None = None, env: dict | None = None, timeout: float = 600,
        check: bool = True, shell: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, env=env, timeout=timeout, check=check, shell=shell,
                          capture_output=True, text=True)


def base_checkout(repo: dict) -> Path:
    """Clone the benchmark repo once and check out the pinned sha (cached)."""
    dst = CACHE_DIR / "repos" / f"{repo['name']}-{repo['sha'][:12]}"
    if (dst / ".git").exists():
        return dst
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(".tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    run(["git", "clone", "--quiet", "--filter=blob:none", "--no-checkout", repo["url"], str(tmp)], timeout=900)
    run(["git", "-C", str(tmp), "checkout", "--quiet", repo["sha"]], timeout=900)
    tmp.rename(dst)
    return dst


def test_venv(repo: dict) -> Path:
    """A venv with the repo's test dependencies (not the repo itself)."""
    venv = CACHE_DIR / "venvs" / repo["name"]
    py = venv / "bin" / "python"
    marker = venv / ".deps"
    want = "\n".join(repo.get("test_deps", []))
    if py.exists() and marker.exists() and marker.read_text() == want:
        return venv
    env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
    run(["uv", "venv", "--quiet", "--python", "3.12", str(venv)], env=env, timeout=600)
    if repo.get("test_deps"):
        run(["uv", "pip", "install", "--quiet", "--python", str(py), *repo["test_deps"]], env=env, timeout=900)
    marker.write_text(want)
    return venv


def apply_setup(workdir: Path, setup: list[dict]) -> None:
    for step in setup:
        p = workdir / step["file"]
        text = p.read_text()
        n = text.count(step["old"])
        if n != 1:
            raise ValueError(f"setup patch for {step['file']} matched {n} times (need exactly 1)")
        p.write_text(text.replace(step["old"], step["new"]))


def make_workdir(base: Path, dest: Path, task: Task) -> Path:
    """Fresh copy of the repo with no history, task setup applied and committed."""
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(base, dest, ignore=shutil.ignore_patterns(".git", ".thrift"), symlinks=True)
    apply_setup(dest, task.setup)
    git = ["git", "-C", str(dest), "-c", "user.name=bench", "-c", "user.email=bench@example.invalid",
           "-c", "commit.gpgsign=false"]
    run(git[:3] + ["init", "--quiet"])
    run(git + ["add", "-A"])
    run(git + ["commit", "--quiet", "-m", "snapshot"])
    return dest


def bench_env(venv: Path, extra: dict | None = None) -> dict:
    """Environment where `python -m pytest` uses the bench venv and imports the work dir."""
    env = {k: v for k, v in os.environ.items() if k not in ("VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME")}
    env["PATH"] = f"{venv / 'bin'}{os.pathsep}{env.get('PATH', '')}"
    env["VIRTUAL_ENV"] = str(venv)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.update(extra or {})
    return env


def run_check(task: Task, workdir: Path, venv: Path, timeout: float = 600) -> tuple[bool, str]:
    if not task.check:
        return True, ""
    p = run(task.check, cwd=workdir, env=bench_env(venv), timeout=timeout, check=False, shell=True)
    tail = (p.stdout + p.stderr).strip().splitlines()[-5:]
    return p.returncode == 0, "\n".join(tail)


def judge_answer(task: Task, answer: str) -> bool:
    text = answer or ""
    if any(not re.search(rx, text, re.I) for rx in task.expect_all):
        return False
    if task.expect_any and not any(re.search(rx, text, re.I) for rx in task.expect_any):
        return False
    return True
