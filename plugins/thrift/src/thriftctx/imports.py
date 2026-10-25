"""Import extraction (at index time) and resolution (at query time).

Raw import specs are stored per file; they are resolved against the current
file list only when ``related_files`` is asked, so edges never go stale when
files are added or removed.
"""

from __future__ import annotations

import posixpath
import re
import sys
from collections import defaultdict

_PY_IMPORT = re.compile(r"^\s*import\s+([\w.]+(?:\s+as\s+\w+)?(?:\s*,\s*[\w.]+(?:\s+as\s+\w+)?)*)", re.M)
_PY_FROM = re.compile(r"^\s*from\s+(\.*)([\w.]*)\s+import\s+(\([^)]*\)|[^\n#]+)", re.M)
_JS_FROM = re.compile(r"""(?:import|export)\s[^'"`;]*?from\s*['"]([^'"]+)['"]""")
_JS_BARE = re.compile(r"""(?:^|[;\s])import\s*['"]([^'"]+)['"]""", re.M)
_JS_CALL = re.compile(r"""(?:require|import)\s*\(\s*['"]([^'"]+)['"]\s*\)""")
_C_INCLUDE = re.compile(r'^\s*#\s*include\s*"([^"]+)"', re.M)
_JAVA_IMPORT = re.compile(r"^\s*import\s+(?:static\s+)?([\w.]+(?:\.\*)?)\s*;", re.M)
_RUST_MOD = re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?mod\s+(\w+)\s*;", re.M)
_RUST_USE = re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?use\s+((?:crate|super|self)(?:::\w+)+)", re.M)

_STDLIB = frozenset(getattr(sys, "stdlib_module_names", ())) or frozenset(
    "abc argparse array ast asyncio base64 collections contextlib copy csv dataclasses datetime decimal "
    "enum functools glob hashlib heapq html http importlib inspect io itertools json logging math "
    "multiprocessing operator os pathlib pickle platform queue random re shutil signal socket sqlite3 "
    "string struct subprocess sys tempfile textwrap threading time traceback types typing unittest "
    "urllib uuid warnings weakref xml zipfile".split()
)

JS_EXTS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts", ".d.ts")


def extract_imports(path: str, lang: str | None, text: str) -> list[str]:
    """Return raw import specs for a file (language-specific strings)."""
    specs: list[str] = []
    if lang == "python":
        for m in _PY_IMPORT.finditer(text):
            for part in m.group(1).split(","):
                specs.append(part.strip().split()[0])
        for m in _PY_FROM.finditer(text):
            dots, mod, names = m.group(1), m.group(2), m.group(3)
            names = [n.strip().split()[0] for n in names.strip("()").replace("\n", " ").split(",") if n.strip()]
            base = dots + mod
            specs.append(base or ".")
            # `from pkg import submodule` -> pkg.submodule may be a module
            for n in names:
                if n != "*" and re.fullmatch(r"\w+", n):
                    specs.append((base + "." if mod else base) + n)
    elif lang in ("javascript", "typescript", "tsx"):
        for rx in (_JS_FROM, _JS_BARE, _JS_CALL):
            specs.extend(m.group(1) for m in rx.finditer(text))
    elif lang in ("c", "cpp"):
        specs.extend(m.group(1) for m in _C_INCLUDE.finditer(text))
    elif lang == "java":
        specs.extend(m.group(1) for m in _JAVA_IMPORT.finditer(text))
    elif lang == "rust":
        specs.extend("mod:" + m.group(1) for m in _RUST_MOD.finditer(text))
        specs.extend(m.group(1) for m in _RUST_USE.finditer(text))
    seen: set[str] = set()
    return [s for s in specs if not (s in seen or seen.add(s))]


class Resolver:
    """Resolves raw import specs to repo files, given the file list."""

    def __init__(self, files: list[str]):
        self.files = set(files)
        self.py_modules: dict[str, list[str]] = defaultdict(list)
        self.by_basename: dict[str, list[str]] = defaultdict(list)
        packages = {posixpath.dirname(f) for f in files if posixpath.basename(f) == "__init__.py"}
        for f in files:
            self.by_basename[posixpath.basename(f)].append(f)
            if f.endswith((".py", ".pyi")):
                mod = f.rsplit(".", 1)[0]
                if mod.endswith("/__init__") or mod == "__init__":
                    mod = posixpath.dirname(mod)
                parts = [p for p in mod.split("/") if p]
                # Importable names start below the deepest non-package dir
                # (like a sys.path root: src/, tests/, the repo root).
                first = 0
                for i in range(len(parts) - 1, 0, -1):
                    if "/".join(parts[:i]) not in packages:
                        first = i
                        break
                for i in range(first + 1):
                    if parts[i:]:
                        self.py_modules[".".join(parts[i:])].append(f)
        for v in self.py_modules.values():
            v.sort(key=lambda p: (p.count("/"), len(p)))
        # top-level modules/packages that live at the repo root (may shadow stdlib)
        self.py_modules_top = {
            f.split("/", 1)[0].rsplit(".", 1)[0] for f in files if f.endswith((".py", ".pyi"))
            and (f.count("/") == 0 or f.split("/", 1)[0] in packages)
        }

    # -- per language -----------------------------------------------------

    def _python(self, src: str, spec: str) -> list[str]:
        if spec.startswith("."):
            dots = len(spec) - len(spec.lstrip("."))
            rest = spec[dots:]
            pkg = posixpath.dirname(src)
            for _ in range(dots - 1):
                pkg = posixpath.dirname(pkg)
            base = posixpath.join(pkg, *rest.split(".")) if rest else pkg
            for cand in (base + ".py", base + ".pyi", posixpath.join(base, "__init__.py")):
                if cand.lstrip("/") in self.files:
                    return [cand.lstrip("/")]
            return []
        top = spec.split(".", 1)[0]
        if top in _STDLIB and top not in self.py_modules_top:
            return []  # `import json` is the stdlib, not some utils/json.py
        hits = self.py_modules.get(spec)
        return hits[:1] if hits else []

    def _js(self, src: str, spec: str) -> list[str]:
        if not spec.startswith("."):
            return []
        base = posixpath.normpath(posixpath.join(posixpath.dirname(src), spec))
        cands = [base] + [base + e for e in JS_EXTS]
        # TS ESM style: import './x.js' that is really x.ts
        stem, ext = posixpath.splitext(base)
        if ext in (".js", ".jsx", ".mjs", ".cjs"):
            cands += [stem + e for e in JS_EXTS]
        cands += [posixpath.join(base, "index" + e) for e in JS_EXTS]
        for c in cands:
            if c in self.files:
                return [c]
        return []

    def _c(self, src: str, spec: str) -> list[str]:
        local = posixpath.normpath(posixpath.join(posixpath.dirname(src), spec))
        if local in self.files:
            return [local]
        if spec in self.files:
            return [spec]
        cands = [f for f in self.by_basename.get(posixpath.basename(spec), []) if f.endswith(spec)]
        return sorted(cands, key=len)[:1]

    def _java(self, src: str, spec: str) -> list[str]:
        if spec.endswith(".*"):
            d = spec[:-2].replace(".", "/")
            return sorted(f for f in self.files if f.endswith(".java") and posixpath.dirname(f).endswith(d))[:10]
        parts = spec.split(".")
        # static imports / nested classes: try progressively shorter paths
        for i in range(len(parts), 0, -1):
            tail = "/".join(parts[:i]) + ".java"
            cands = [f for f in self.by_basename.get(parts[i - 1] + ".java", []) if f.endswith(tail)]
            if cands:
                return sorted(cands, key=len)[:1]
        return []

    def _rust(self, src: str, spec: str) -> list[str]:
        d = posixpath.dirname(src)
        stem = posixpath.splitext(posixpath.basename(src))[0]
        mod_dir = d if stem in ("lib", "main", "mod") else posixpath.join(d, stem)
        if spec.startswith("mod:"):
            name = spec[4:]
            for c in (posixpath.join(mod_dir, name + ".rs"), posixpath.join(mod_dir, name, "mod.rs")):
                if c in self.files:
                    return [c]
            return []
        parts = spec.split("::")
        head, rest = parts[0], parts[1:]
        if head == "crate":
            # find the crate root: nearest ancestor dir containing src/lib.rs or src/main.rs
            root = d
            while root and not (posixpath.basename(root) == "src"):
                root = posixpath.dirname(root)
            base = root or d
        elif head == "super":
            base = posixpath.dirname(mod_dir)
        else:
            base = mod_dir
        for i in range(len(rest), 0, -1):
            p = posixpath.join(base, *rest[:i])
            for c in (p + ".rs", posixpath.join(p, "mod.rs")):
                if c in self.files:
                    return [c]
        return []

    def resolve(self, src: str, lang: str | None, spec: str) -> list[str]:
        if lang == "python":
            out = self._python(src, spec)
        elif lang in ("javascript", "typescript", "tsx"):
            out = self._js(src, spec)
        elif lang in ("c", "cpp"):
            out = self._c(src, spec)
        elif lang == "java":
            out = self._java(src, spec)
        elif lang == "rust":
            out = self._rust(src, spec)
        else:
            out = []
        return [f for f in out if f != src]
