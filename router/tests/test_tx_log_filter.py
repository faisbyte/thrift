import re

import pytest

import reprouter.transforms.log_filter  # noqa: F401  (registers the transform)
from reprouter.classify import Label
from reprouter.request import Block
from reprouter.transforms.base import Context, get

FOOTER = re.compile(r"\[reprouter: filtered (\d+)→(\d+) lines\. Kept: every line .*"
                    r"Re-run the command to see the full output\.\]$")
OMITTED = re.compile(r"^\[… (\d+) lines omitted …\]$")

MODULES = ["test_api", "test_auth", "test_billing", "test_dates", "test_models", "test_search"]


def _progress(failing=()):
    """148 verbose pytest progress lines; `failing` holds (module, test) pairs that FAIL."""
    out, total, i = [], 148, 0
    for m, mod in enumerate(MODULES):
        for k in range(25 if m < 4 else 24):
            i += 1
            name = f"test_{k:02d}"
            for fmod, fname in failing:
                if fmod == mod and k == 7 + m:
                    name = fname
            status = "FAILED" if (mod, name) in failing else "PASSED"
            out.append(f"tests/{mod}.py::{name} {status}".ljust(73) + f"[{100 * i // total:3d}%]")
    return out


HEAD = [
    "============================= test session starts ==============================",
    "platform darwin -- Python 3.12.4, pytest-8.3.2, pluggy-1.5.0 -- /Users/dev/proj/.venv/bin/python",
    "cachedir: .pytest_cache",
    "rootdir: /Users/dev/proj",
    "configfile: pyproject.toml",
    "plugins: anyio-4.4.0, cov-5.0.0",
    "collecting ... collected 148 items",
    "",
]

FAILURES = [
    "",
    "=================================== FAILURES ===================================",
    "_______________________________ test_parse_date ________________________________",
    "",
    "    def test_parse_date():",
    '        d = parse_date("2024-02-30")',
    ">       assert d.day == 30",
    "E       AssertionError: assert 1 == 30",
    "E        +  where 1 = datetime.date(2024, 3, 1).day",
    "",
    "tests/test_dates.py:14: AssertionError",
    "_____________________________ test_user_roundtrip ______________________________",
    "",
    "    def test_user_roundtrip():",
    '        u = User(name="ada", email="ada@example.com")',
    ">       assert User.from_json(u.to_json()) == u",
    "E       TypeError: from_json() missing 1 required positional argument: 'schema'",
    "",
    "tests/test_models.py:41: TypeError",
    "=========================== short test summary info ============================",
    "FAILED tests/test_dates.py::test_parse_date - AssertionError: assert 1 == 30",
    "FAILED tests/test_models.py::test_user_roundtrip - TypeError: from_json() missing 1 required positional "
    "argument: 'schema'",
    "======================== 2 failed, 146 passed in 4.21s =========================",
]


def pytest_failing_run():
    failing = (("test_dates", "test_parse_date"), ("test_models", "test_user_roundtrip"))
    return "\n".join(HEAD + _progress(failing) + FAILURES) + "\n"


def pytest_passing_run():
    tail = ["", "============================= 148 passed in 3.87s =============================="]
    return "\n".join(HEAD + _progress() + tail) + "\n"


def jest_passing_run():
    out = ["", "> shop@1.4.0 test", "> jest --verbose", ""]
    for suite in ["cart", "user", "orders", "pricing", "search", "auth"]:
        out += [f" PASS  src/{suite}.test.js", f"  {suite.title()}"]
        out += [f"    ✓ handles case {k} ({k % 4 + 1} ms)" for k in range(7)]
        out.append("")
    out += ["Test Suites: 6 passed, 6 total", "Tests:       42 passed, 42 total", "Snapshots:   0 total",
            "Time:        2.481 s", "Ran all test suites."]
    return "\n".join(out) + "\n"


JEST_FAILURE = [
    " FAIL  src/cart.test.js",
    "  Cart",
    "    ✓ adds items (3 ms)",
    "    ✕ applies discount (5 ms)",
    "    ✓ removes items (1 ms)",
    "",
    "  ● Cart › applies discount",
    "",
    "    expect(received).toBe(expected) // Object.is equality",
    "",
    "    Expected: 90",
    "    Received: 100",
    "",
    "      12 |   const cart = new Cart();",
    "      13 |   cart.add({ price: 100 });",
    "    > 14 |   expect(cart.total({ discount: 0.1 })).toBe(90);",
    "         |                                         ^",
    "      15 | });",
    "      16 |",
    "",
    "      at Object.toBe (src/cart.test.js:14:41)",
    "",
]


def jest_failing_run():
    out = ["", "> shop@1.4.0 test", "> jest --verbose", ""]
    for i, suite in enumerate(["user", "orders", "pricing", "search", "auth"]):
        if i == 3:
            out += JEST_FAILURE
        out += [f" PASS  src/{suite}.test.js", f"  {suite.title()}"]
        out += [f"    ✓ handles case {k} ({k % 4 + 1} ms)" for k in range(7)]
        out.append("")
    out += ["Test Suites: 1 failed, 5 passed, 6 total", "Tests:       1 failed, 38 passed, 39 total",
            "Snapshots:   0 total", "Time:        2.913 s", "Ran all test suites."]
    return "\n".join(out) + "\n"


TRACEBACK = [
    "2024-10-25 10:00:07,456 ERROR [worker-3] Job 1047 failed",
    "Traceback (most recent call last):",
    '  File "/app/worker.py", line 88, in run',
    "    result = handler(job)",
    "             ^^^^^^^^^^^^",
    '  File "/app/handlers/export.py", line 42, in handle',
    "    rows = fetch_rows(job.query)",
    "           ^^^^^^^^^^^^^^^^^^^^^",
    '  File "/app/db.py", line 17, in fetch_rows',
    "    cur.execute(sql)",
    "    ~~~~~~~~~~~^^^^^",
    'psycopg2.errors.UndefinedTable: relation "exports" does not exist',
]


def worker_log():
    out = ["2024-10-25 10:00:00,001 INFO  [main] Starting export-worker v2.3.1",
           "2024-10-25 10:00:00,014 INFO  [main] Loaded config from /etc/export-worker.yaml"]
    for job in range(1001, 1047):
        out.append(f"2024-10-25 10:00:0{job % 7},{job % 1000:03d} INFO  [worker-{job % 4}] Processing job {job}")
    out += TRACEBACK
    for job in range(1048, 1090):
        out.append(f"2024-10-25 10:00:0{job % 7},{job % 1000:03d} INFO  [worker-{job % 4}] Job {job} done in "
                   f"{job % 90 + 10}ms")
    return "\n".join(out) + "\n"


def cargo_passing_run():
    out = ["   Compiling shop v0.1.0 (/Users/dev/shop)",
           "    Finished `test` profile [unoptimized + debuginfo] target(s) in 1.92s",
           "     Running unittests src/lib.rs (target/debug/deps/shop-3f2a9c1d)", "", "running 40 tests"]
    out += [f"test cart::tests::handles_case_{k} ... ok" for k in range(40)]
    out += ["", "test result: ok. 40 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.01s", ""]
    return "\n".join(out) + "\n"


def go_passing_run():
    out = []
    for k in range(20):
        out += [f"=== RUN   TestCart{k}", f"--- PASS: TestCart{k} (0.00s)"]
    out += ["PASS", "ok  \tgithub.com/dev/shop/cart\t0.014s"]
    return "\n".join(out) + "\n"


def result_block(text, tool="Bash"):
    return Block(("messages", 4, "content", 0), "user", "tool_result", text, msg_index=4, tool_name=tool,
                 tool_input={"command": "pytest -v"},
                 raw={"type": "tool_result", "tool_use_id": "tu_2", "content": text})


def run(text, kind="tool_result:test_output"):
    return get("log_filter").run(result_block(text), Label(kind), Context())


def test_failing_pytest_run_keeps_failure_details_and_drops_passing_lines():
    res = run(pytest_failing_run())
    out = res.content
    assert "E       AssertionError: assert 1 == 30" in out
    assert "E       TypeError: from_json() missing 1 required positional argument: 'schema'" in out
    assert "tests/test_dates.py:14: AssertionError" in out
    assert "FAILED tests/test_dates.py::test_parse_date - AssertionError: assert 1 == 30" in out
    assert "======================== 2 failed, 146 passed in 4.21s =========================" in out
    assert "tests/test_api.py::test_05 PASSED" not in out
    assert FOOTER.search(out)


def body_lines(out):
    return out.split("\n")[:-1]  # drop the footer


def test_keeps_first_three_and_last_three_lines():
    text = pytest_failing_run()
    src = text.split("\n")[:-1]
    out = body_lines(run(text).content)
    assert out[:3] == src[:3]
    assert out[-3:] == src[-3:]


def test_single_line_gap_shows_the_line_instead_of_a_marker():
    out = run(pytest_failing_run()).content
    assert "    def test_parse_date():" in out
    assert "[… 1 lines omitted …]" not in out


def test_footer_counts_match_kept_and_omitted_lines():
    text = pytest_failing_run()
    out = body_lines(run(text).content)
    orig, kept = map(int, FOOTER.search(run(text).content).groups())
    omitted = sum(int(m.group(1)) for ln in out if (m := OMITTED.match(ln)))
    shown = sum(1 for ln in out if not OMITTED.match(ln))
    assert orig == len(text.split("\n")) - 1
    assert kept == shown
    assert kept + omitted == orig


def test_all_pass_pytest_run_collapses_to_summary_and_footer():
    out = run(pytest_passing_run()).content
    shown = [ln for ln in body_lines(out) if not OMITTED.match(ln)]
    assert shown == ["============================= 148 passed in 3.87s =============================="]
    assert FOOTER.search(out).groups() == ("158", "1")


@pytest.mark.parametrize("make, summary", [
    (jest_passing_run, ["Test Suites: 6 passed, 6 total", "Tests:       42 passed, 42 total"]),
    (cargo_passing_run, ["test result: ok. 40 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; "
                         "finished in 0.01s"]),
    (lambda: cargo_passing_run().replace("; finished in 0.01s", ""),  # cargo < 1.59 prints no timing
     ["test result: ok. 40 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out"]),
    (go_passing_run, ["PASS", "ok  \tgithub.com/dev/shop/cart\t0.014s"]),
])
def test_all_pass_runs_of_other_runners_collapse_to_their_summary(make, summary):
    out = run(make()).content
    assert [ln for ln in body_lines(out) if not OMITTED.match(ln)] == summary


def test_failing_jest_run_keeps_the_failure_block_and_summary():
    out = run(jest_failing_run()).content
    for line in [" FAIL  src/cart.test.js", "    ✕ applies discount (5 ms)", "  ● Cart › applies discount",
                 "    Expected: 90", "    Received: 100",
                 "    > 14 |   expect(cart.total({ discount: 0.1 })).toBe(90);",
                 "      at Object.toBe (src/cart.test.js:14:41)",
                 "Test Suites: 1 failed, 5 passed, 6 total", "Tests:       1 failed, 38 passed, 39 total"]:
        assert line in out.split("\n"), line
    assert " PASS  src/orders.test.js" not in out


def test_log_keeps_whole_traceback_through_the_exception_line():
    out = run(worker_log(), kind="tool_result:log").content
    assert "\n".join(TRACEBACK) in out
    assert "Processing job 1020" not in out
    assert "Job 1070 done" not in out


def info_log(n, errors=()):
    lines = [f"2024-10-25 10:{i // 60:02d}:{i % 60:02d} INFO  request {i} served in {i % 50 + 5}ms" for i in range(n)]
    for i in errors:
        lines[i] = f"2024-10-25 10:{i // 60:02d}:{i % 60:02d} ERROR upstream timeout on request {i}"
    return "\n".join(lines) + "\n"


def test_go_panic_line_is_kept():
    lines = [f"2024/10/25 10:00:{i % 60:02d} handled /api/items/{i} in {i % 9 + 1}ms" for i in range(90)]
    lines[50:50] = ["panic: runtime error: index out of range [5] with length 3", "",
                    "goroutine 1 [running]:", "main.lookup(...)",
                    "\t/Users/dev/app/main.go:12 +0x1d", "exit status 2"]
    out = run("\n".join(lines) + "\n", kind="tool_result:command_output").content
    assert "panic: runtime error: index out of range [5] with length 3" in out
    assert "\t/Users/dev/app/main.go:12 +0x1d" in out


def test_compiler_error_lines_are_kept():
    lines = [f"   Compiling dep-{i} v0.{i}.0" for i in range(85)]
    lines[40:40] = ["error[E0308]: mismatched types", "", "", "  |", "4 |     let x: i32 = \"a\";",
                    "  |                  ^^^ expected `i32`, found `&str`", "  |", "  = note: see E0308",
                    "  --> src/main.rs:4:18"]
    lines[70:70] = ["src/cart.ts(12,5): error TS2322: Type 'string' is not assignable to type 'number'."]
    out = run("\n".join(lines) + "\n", kind="tool_result:command_output").content
    assert "error[E0308]: mismatched types" in out
    assert "src/cart.ts(12,5): error TS2322: Type 'string' is not assignable to type 'number'." in out


def test_keeps_exactly_two_lines_of_context_around_a_problem():
    out = run(info_log(60, errors=[30]), kind="tool_result:log").content
    for i in range(28, 33):
        assert re.search(rf"request {i}\b", out), i
    for i in (27, 33):
        assert not re.search(rf"request {i}\b", out), i


def test_declines_fewer_than_thirty_lines():
    assert run(info_log(29, errors=[14]), kind="tool_result:log") is None
    assert run(info_log(30, errors=[14]), kind="tool_result:log") is not None


def test_declines_when_filtering_saves_too_little():
    assert run(info_log(40, errors=range(0, 40, 3)), kind="tool_result:log") is None


def test_command_output_needs_at_least_eighty_lines():
    assert run(info_log(79, errors=[40]), kind="tool_result:command_output") is None
    assert run(info_log(80, errors=[40]), kind="tool_result:command_output") is not None


@pytest.mark.parametrize("kind", ["tool_result:file_read", "tool_result:search_results", "tool_result:json",
                                  "tool_result:error", "tool_result:diff"])
def test_declines_other_result_kinds(kind):
    assert run(pytest_failing_run(), kind=kind) is None


def test_declines_non_tool_result_blocks():
    blk = Block(("messages", 3, "content", 0), "assistant", "text", pytest_failing_run(), msg_index=3)
    assert get("log_filter").run(blk, Label("assistant_text"), Context()) is None


def test_declines_results_with_non_text_parts():
    blk = result_block(pytest_failing_run())
    blk.raw = {"type": "tool_result", "tool_use_id": "tu_2", "content": [
        {"type": "text", "text": blk.text},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "iVBORw0K"}},
    ]}
    assert get("log_filter").run(blk, Label("tool_result:test_output"), Context()) is None


def test_is_lossy_and_deterministic():
    assert get("log_filter").lossless is False
    outs = [run(pytest_failing_run()) for _ in range(5)]
    assert outs[0].lossless is False
    assert len({o.content for o in outs}) == 1


# --- regression: generic command output must not be filtered unless the command is a test/build/log runner

def _cmd_block(text, command):
    from reprouter.request import Block

    return Block(("messages", 3, "content", 0), "user", "tool_result", text, msg_index=3, tool_name="Bash",
                 tool_input={"command": command} if command is not None else {})


_CODE_LISTING = "\n".join(
    f"{i}\tthrow new Error('bad input ' + x{i});" if i % 25 == 0 else f"{i}\tconst value{i} = compute({i});"
    for i in range(1, 140))


def test_file_viewing_commands_are_never_filtered():
    from reprouter.classify import Label
    from reprouter.transforms.base import Context
    from reprouter.transforms.log_filter import LogFilter

    for cmd in ("cat -n app/page.tsx", "sed -n 120,380p app/page.tsx", "head -200 src/x.ts",
                "git show HEAD:src/x.ts", "ls -la /tmp", "git ls-files | head -200",
                "python3 - <<'EOF'\nprint(open('x').read())\nEOF"):
        assert LogFilter().run(_cmd_block(_CODE_LISTING, cmd), Label("tool_result:command_output"), Context()) is None, cmd


def test_command_output_without_a_known_command_is_not_filtered():
    from reprouter.classify import Label
    from reprouter.transforms.base import Context
    from reprouter.transforms.log_filter import LogFilter

    assert LogFilter().run(_cmd_block(_CODE_LISTING, None), Label("tool_result:command_output"), Context()) is None


def test_build_and_test_runner_command_output_is_filtered():
    from reprouter.classify import Label
    from reprouter.transforms.base import Context
    from reprouter.transforms.log_filter import LogFilter

    build = "\n".join([f"compiling crate_{i} v0.1.{i}" for i in range(120)]
                      + ["error[E0308]: mismatched types", "  --> src/main.rs:14:5", "error: could not compile `app`"])
    for cmd in ("cargo build 2>&1", "cd app && npm run build", "make -j8", "tail -n 500 /var/log/app.log"):
        assert LogFilter().run(_cmd_block(build, cmd), Label("tool_result:command_output"), Context()) is not None, cmd


def test_footer_states_that_every_problem_line_was_kept():
    from reprouter.transforms.log_filter import FOOTER

    text = FOOTER.format(orig=300, kept=20)
    assert "every" in text and "ERROR" in text and "FAIL" in text and "removed" in text
