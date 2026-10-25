"""Lossy filter for long test/log output: keep failures and errors, drop the rest."""

from __future__ import annotations

import re

from .base import Context, Result, Transform, register

CONTEXT = 2  # lines kept on each side of a problem line
EDGE = 3  # first/last lines always kept
MIN_LINES = 30
COMMAND_MIN_LINES = 80  # generic command output is only filtered when it is long
MAX_RATIO = 0.6  # decline unless the result is at most 60% of the input

_KINDS = frozenset({"tool_result:test_output", "tool_result:log", "tool_result:command_output"})

_SRC_EXT = (r"py|pyi|js|jsx|mjs|cjs|ts|tsx|mts|cts|vue|svelte|rs|go|java|kt|kts|scala|groovy|c|h|cc|cpp|cxx|hpp|hh"
            r"|m|mm|swift|cs|fs|rb|php|pl|pm|lua|ex|exs|erl|hrl|clj|cljs|dart|zig|nim|sh|bash|zsh|sql|r|jl")
# Case-sensitive on purpose: "ERROR"/"Error" are signals, while a lowercase "error" counts only
# in compiler diagnostic form ("error:", "error[E0308]:", "error TS2322:"), not in prose.
_PROBLEM = re.compile(
    r"\bFAIL(?:ED|URES?)?\b|\bERROR\b|Error\b|Exception\b|\bTraceback\b"
    r"|^E {3}|^> {3}"  # pytest assertion detail / failing source line
    r"|[✕✗✖●]|^\s*[-+]?\s*(?:Expected|Received)\b|^\s*> \d+ \|"  # jest / vitest failure block
    r"|^panic:|\bpanicked at\b"  # go / rust
    r"|\berror(?:\[\w+\])?:|\berror TS\d+:"  # gcc/clang, rustc, tsc diagnostics
    rf"|[\w./\\-]+\.(?:{_SRC_EXT})(?::\d+|\(\d+,\d+\))"  # file:line / file(line,col) reference
)
_TRACEBACK = re.compile(r"^\s*Traceback \(most recent call last\):")
_SUMMARY = re.compile(
    r"\b\d+ (?:passed|failed|errors?)\b.*\bin [\d.]+s\b"  # pytest
    r"|^Test Suites:\s|^Tests:\s"  # jest / vitest
    r"|^test result:\s"  # cargo
    r"|^(?:PASS|FAIL)$|^(?:ok|FAIL)\s+\S+\s+(?:[\d.]+s|\(cached\))"  # go
)

FOOTER = ("[reprouter: filtered {orig}→{kept} lines. Kept: every line mentioning ERROR/FAIL/Exception/Traceback/"
          "assertions, their context, and summary lines; passing/INFO lines were removed. Re-run the command to see "
          "the full output.]")


class LogFilter(Transform):
    """Keep problem lines (+context), tracebacks, runner summaries and the head/tail."""

    name = "log_filter"
    lossless = False

    def applicable(self, block, label, ctx: Context) -> bool:
        if block.type != "tool_result" or label.kind not in _KINDS or not _text_only(block):
            return False
        if label.kind != "tool_result:command_output":
            return True
        # generic command output: only when the command is a test/build/lint/log runner. File viewers
        # (cat, sed -n, head, git show, ls, scripts printing files) must never be filtered.
        return len(_lines(block.text)) >= COMMAND_MIN_LINES and is_runner_command(
            (block.tool_input or {}).get("command", ""))

    def apply(self, block, label, ctx: Context) -> Result | None:
        lines = _lines(block.text)
        if len(lines) < MIN_LINES:
            return None
        keep = _select(lines)
        out = _render(lines, keep)
        out.append(FOOTER.format(orig=len(lines), kept=len(keep)))
        content = "\n".join(out)
        if len(content) > MAX_RATIO * len(block.text):
            return None
        return Result(content=content, lossless=False)


_RUNNER = re.compile(
    r"^(?:python3?\s+-m\s+(?:pytest|unittest|tox|mypy|ruff|pylint|flake8)|py\.test|pytest|tox|nox"
    r"|(?:npm|pnpm|yarn|bun)\s+(?:test|t|ci|install|run\s+(?:test|build|lint|check|typecheck)\S*)"
    r"|npx\s+(?:jest|vitest|tsc|eslint|playwright|mocha)|jest|vitest|mocha|tsc|eslint|ruff|mypy|flake8|pylint"
    r"|go\s+(?:test|build|vet)|cargo\s+(?:test|build|check|clippy|run)|make|cmake|ninja|gradle|\./gradlew|mvn"
    r"|dotnet\s+(?:test|build)|terraform\s+(?:plan|apply|init|validate)|docker\s+(?:logs|build|compose)"
    r"|kubectl\s+logs|journalctl|tail\s+.*(?:\.log\b|/log/|-f\b))"
)
_ENV_ASSIGN = re.compile(r"^(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)+")


def is_runner_command(command: str) -> bool:
    """True if any pipeline/compound segment starts with a test, build, lint or log-viewing runner."""
    for seg in re.split(r"&&|\|\||;|\||\n", command or ""):
        seg = _ENV_ASSIGN.sub("", seg.strip())
        if seg and not seg.startswith("cd ") and _RUNNER.match(seg):
            return True
    return False


def _lines(text: str) -> list[str]:
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()  # a trailing newline is not an extra line
    return lines


def _select(lines: list[str]) -> set[int]:
    """Indices of the lines to keep."""
    n = len(lines)
    problems = [i for i, ln in enumerate(lines) if _PROBLEM.search(ln)]
    summaries = {i for i, ln in enumerate(lines) if _SUMMARY.search(ln)}
    if not problems and summaries:
        keep = set(summaries)  # all-pass run: the summary says it all
    else:
        keep = summaries | set(range(min(EDGE, n))) | set(range(max(0, n - EDGE), n))
        for i in problems:
            keep.update(range(max(0, i - CONTEXT), min(n, i + CONTEXT + 1)))
            if _TRACEBACK.match(lines[i]):
                keep.update(range(i, _traceback_end(lines, i) + 1))
    # a marker line is no shorter than the one line it would replace
    keep.update(i for i in range(1, n - 1) if i - 1 in keep and i + 1 in keep)
    return keep


def _text_only(block) -> bool:
    """A string replacement would drop images or other non-text parts."""
    content = block.raw.get("content") if isinstance(block.raw, dict) else None
    return not isinstance(content, list) or all(
        isinstance(c, dict) and c.get("type") == "text" for c in content)


def _traceback_end(lines: list[str], start: int) -> int:
    """Index of the exception line closing a Python traceback: the first unindented line."""
    j = start + 1
    while j < len(lines) and lines[j][:1] in (" ", "\t"):
        j += 1
    return min(j, len(lines) - 1)


def _render(lines: list[str], keep: set[int]) -> list[str]:
    out: list[str] = []
    gap = 0
    for i, ln in enumerate(lines):
        if i not in keep:
            gap += 1
            continue
        if gap:
            out.append(f"[… {gap} lines omitted …]")
            gap = 0
        out.append(ln)
    if gap:
        out.append(f"[… {gap} lines omitted …]")
    return out


register(LogFilter())
