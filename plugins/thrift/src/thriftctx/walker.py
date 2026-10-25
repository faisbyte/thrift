"""Enumerate indexable files in a project.

Uses ``git ls-files`` when the project is a git work tree (exact .gitignore
semantics, fast). Otherwise walks the tree, honouring nested .gitignore
files via ``pathspec``.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

SKIP_DIRS = frozenset(
    {
        ".git", ".hg", ".svn", ".thrift", "node_modules", "vendor", "third_party",
        "__pycache__", ".venv", "venv", ".tox", ".nox", ".mypy_cache", ".pytest_cache",
        ".ruff_cache", "site-packages", ".idea", ".vscode", ".gradle",
    }
)
# Only skipped when we can't ask git (i.e. likely-generated dirs).
SKIP_DIRS_NO_GIT = frozenset({"dist", "build", "target", "out", ".next", "coverage", ".cache"})

LOCKFILES = frozenset(
    {
        "package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml",
        "bun.lockb", "poetry.lock", "uv.lock", "Pipfile.lock", "Cargo.lock",
        "Gemfile.lock", "composer.lock", "go.sum", "flake.lock", "pdm.lock",
    }
)

BINARY_EXTS = frozenset(
    """
    png jpg jpeg gif bmp ico icns webp avif tiff psd svg pdf
    zip gz tgz bz2 xz 7z rar tar jar war ear whl egg
    so dylib dll exe o a lib obj class pyc pyo pyd wasm bin dat db sqlite sqlite3
    ttf otf woff woff2 eot mp3 mp4 m4a wav ogg flac mov avi mkv webm
    parquet feather arrow npy npz pkl pickle h5 onnx pt ckpt safetensors
    map lockb min.js min.css
    """.split()
)
DATA_EXTS = frozenset({"csv", "tsv", "jsonl", "ndjson", "log"})
# Structured/config text: indexed but with a tighter size cap.
CONFIG_EXTS = frozenset({"json", "yaml", "yml", "toml", "xml", "ini", "cfg"})

MAX_BYTES = 1_000_000
MAX_CONFIG_BYTES = 200_000
SNIFF_BYTES = 8192


@dataclass(frozen=True)
class FileInfo:
    rel: str  # posix-style path relative to root
    mtime_ns: int
    size: int


def _ext(name: str) -> str:
    low = name.lower()
    if low.endswith((".min.js", ".min.css")):
        return "min." + low.rsplit(".", 1)[1]
    return low.rsplit(".", 1)[1] if "." in low else ""


def skip_by_name(rel: str) -> bool:
    name = rel.rsplit("/", 1)[-1]
    if name in LOCKFILES or name.endswith(".lock"):
        return True
    ext = _ext(name)
    if ext in BINARY_EXTS or ext in DATA_EXTS:
        return True
    parts = rel.split("/")[:-1]
    return any(p in SKIP_DIRS for p in parts) or (
        len(parts) >= 2 and parts[0] == ".claude" and parts[1] == "worktrees"
    )


#: hand-written source: long lines (big string literals) are not "minified"
NEVER_MINIFIED_EXTS = frozenset(
    {"py", "pyi", "rs", "java", "c", "h", "cc", "cpp", "cxx", "hpp", "hh", "go", "rb", "md", "rst", "txt"}
)


def looks_binary_or_minified(head: bytes, rel: str = "") -> bool:
    if b"\x00" in head:
        return True
    if _ext(rel.rsplit("/", 1)[-1]) in NEVER_MINIFIED_EXTS:
        return False
    lines = head.split(b"\n")
    if len(head) >= 2048 and len(lines) <= 3:
        return True  # a few very long lines: minified / generated
    return max((len(ln) for ln in lines), default=0) > 5000


def _git_files(root: Path) -> list[str] | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            capture_output=True,
            timeout=30,
            check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return [p for p in out.decode("utf-8", "surrogateescape").split("\0") if p]


def _walk_files(root: Path) -> list[str]:
    try:
        import pathspec
    except ImportError:  # pragma: no cover
        pathspec = None

    results: list[str] = []
    # stack of (dir_rel, [(base_rel, spec), ...])
    stack: list[tuple[str, list]] = [("", [])]
    while stack:
        rel_dir, specs = stack.pop()
        abs_dir = root / rel_dir if rel_dir else root
        local_specs = specs
        gi = abs_dir / ".gitignore"
        if pathspec is not None and gi.is_file():
            try:
                spec = pathspec.GitIgnoreSpec.from_lines(gi.read_text(errors="replace").splitlines())
                local_specs = specs + [(rel_dir, spec)]
            except Exception:
                pass
        try:
            entries = list(os.scandir(abs_dir))
        except OSError:
            continue
        for e in entries:
            rel = f"{rel_dir}/{e.name}" if rel_dir else e.name
            is_dir = e.is_dir(follow_symlinks=False)
            if is_dir and (e.name in SKIP_DIRS or e.name in SKIP_DIRS_NO_GIT):
                continue
            ignored = False
            for base, spec in local_specs:
                sub = rel[len(base) + 1 :] if base else rel
                if spec.match_file(sub + ("/" if is_dir else "")):
                    ignored = True
                    break
            if ignored:
                continue
            if is_dir:
                stack.append((rel, local_specs))
            elif e.is_file(follow_symlinks=False):
                results.append(rel)
    return results


def list_files(root: str | os.PathLike) -> list[FileInfo]:
    """All indexable files under ``root`` with mtime/size (no content reads)."""
    root = Path(root)
    rels = _git_files(root)
    if rels is None:
        rels = _walk_files(root)
    out: list[FileInfo] = []
    for rel in rels:
        rel = rel.replace(os.sep, "/")
        if skip_by_name(rel):
            continue
        try:
            st = os.stat(root / rel)
        except OSError:
            continue  # deleted but still in git index, broken symlink, ...
        if not os.path.isfile(root / rel):
            continue
        limit = MAX_CONFIG_BYTES if _ext(rel) in CONFIG_EXTS else MAX_BYTES
        if st.st_size == 0 or st.st_size > limit:
            continue
        out.append(FileInfo(rel, st.st_mtime_ns, st.st_size))
    out.sort(key=lambda f: f.rel)
    return out
