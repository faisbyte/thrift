import shutil
import subprocess
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixture_repo(tmp_path: Path) -> Path:
    """A copy of tests/fixtures as a throwaway project (not a git repo)."""
    dst = tmp_path / "proj"
    shutil.copytree(FIXTURES, dst)
    return dst


def write(root: Path, rel: str, text: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def git_init(root: Path) -> None:
    subprocess.run(["git", "init", "-q", str(root)], check=True)
