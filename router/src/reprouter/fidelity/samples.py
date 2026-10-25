"""Probe samples with answers known by construction.

Paid probes only ever use synthetic data or files from the pinned benchmark
repo (never the user's transcripts). Each sample is a tool result, optionally
preceded by earlier tool results (``prior``, for dedup / diff-on-reread), plus
probes of three kinds:
  exact   - a string that must be reproduced verbatim
  number  - a numeric answer
  absent  - asks about something not in the output; the answer is NOT PRESENT
Probes carry a ``focus`` so lossy transforms can be judged on what they
promise to keep ("errors", "signatures") vs what they drop ("general", "body").
"""

from __future__ import annotations

import json
import random
import re
from dataclasses import dataclass, field
from pathlib import Path

WORDS = ["anchor", "beacon", "cobalt", "delta", "ember", "falcon", "garnet", "harbor", "indigo", "jasper",
         "kestrel", "lumen", "marble", "nimbus", "onyx", "pylon", "quartz", "raven", "sable", "tundra"]


@dataclass
class Probe:
    id: str
    kind: str
    question: str
    answer: str
    focus: str = "general"
    subject: str = ""


@dataclass
class Sample:
    id: str
    family: str
    tool_name: str
    tool_input: dict
    text: str
    probes: list = field(default_factory=list)
    prior: list = field(default_factory=list)  # [{"tool_name", "tool_input", "text"}]
    project_root: str = "/work/proj"

    def full_text(self) -> str:
        return "\n".join([p["text"] for p in self.prior] + [self.text])


def _hex(rng: random.Random, n: int) -> str:
    return "".join(rng.choice("0123456789abcdef") for _ in range(n))


# ----------------------------------------------------------------------------- JSON records

def json_record_samples(n: int = 4, seed: int = 0) -> list[Sample]:
    out = []
    for si in range(n):
        rng = random.Random(seed * 1000 + si)
        skus = rng.sample(range(1000, 9999), 45)
        recs = [{"id": 1000 + i, "sku": f"SKU-{skus[i]}", "name": f"{rng.choice(WORDS)}-{rng.choice(WORDS)}",
                 "price": round(rng.uniform(1, 500), 2), "qty": rng.randint(0, 90),
                 "warehouse": rng.choice(["east", "west", "north", "south"]), "sha": _hex(rng, 12)}
                for i in range(40)]
        text = json.dumps(recs, indent=2)
        a, b, c = rng.sample(recs, 3)
        wh = rng.choice(["east", "west", "north", "south"])
        missing = f"SKU-{skus[44]}"
        probes = [
            Probe("sha", "exact", f"What is the sha of the item with sku {a['sku']}?", a["sha"]),
            Probe("name", "exact", f"What is the name of the item with id {b['id']}?", b["name"]),
            Probe("price", "number", f"What is the price of the item with sku {c['sku']}?", json.dumps(c["price"])),
            Probe("sumqty", "number", f"What is the total qty of all items in warehouse {wh}?",
                  str(sum(r["qty"] for r in recs if r["warehouse"] == wh))),
            Probe("count", "number", "How many items are in the list?", str(len(recs))),
            Probe("absent", "absent", f"What is the price of the item with sku {missing}?", "NOT PRESENT",
                  subject=missing),
        ]
        out.append(Sample(f"json-{seed}-{si}", "json_records", "Bash", {"command": "curl -s localhost:8080/api/items"},
                          text, probes))
    return out


# ----------------------------------------------------------------------------- pytest runs

def pytest_samples(n: int = 4, seed: int = 0) -> list[Sample]:
    out = []
    for si in range(n):
        rng = random.Random(seed * 1000 + 100 + si)
        mods = rng.sample(["alpha", "beta", "gamma", "delta", "kappa", "omega"], 4)
        tests = [(m, f"test_{m}_{i}") for m in mods for i in range(45)]
        fails = rng.sample(tests, 2)
        color = si % 2 == 1
        lines = ["============================= test session starts ==============================",
                 "platform darwin -- Python 3.12.7, pytest-8.2.2, pluggy-1.5.0",
                 f"collected {len(tests)} items", ""]
        failvals = {}
        for k, (m, t) in enumerate(tests):
            status = "FAILED" if (m, t) in fails else "PASSED"
            if color:
                status = ("\x1b[31m" if status == "FAILED" else "\x1b[32m") + status + "\x1b[0m"
            lines.append(f"tests/test_{m}.py::{t} {status}{' ' * 10}[{int(100 * (k + 1) / len(tests)):3d}%]")
        lines += ["", "=================================== FAILURES ==================================="]
        for m, t in fails:
            got, want = rng.randint(100, 999), rng.randint(100, 999)
            failvals[t] = got
            lines += [f"_____________________________ {t} _____________________________", "",
                      f"    def {t}():", f">       assert compute('{t[-2:]}') == {want}",
                      f"E       AssertionError: assert {got} == {want}", f"E        +  where {got} = compute('{t[-2:]}')",
                      "", f"tests/test_{m}.py:{rng.randint(10, 300)}: AssertionError"]
        lines += ["=========================== short test summary info ============================"]
        lines += [f"FAILED tests/test_{m}.py::{t} - AssertionError" for m, t in fails]
        lines += [f"========================= 2 failed, {len(tests) - 2} passed in {rng.uniform(1, 9):.2f}s ========================="]
        text = "\n".join(lines)
        (fm, ft) = fails[0]
        passed = [x for x in tests if x not in fails]
        pm, pt = rng.choice(passed)
        ghost = "test_zeta_3"
        probes = [
            Probe("failname", "exact", f"Which test failed in tests/test_{fm}.py?", ft, focus="errors"),
            Probe("failval", "number", f"In the failing test {ft}, what value did compute() actually return?",
                  str(failvals[ft]), focus="errors"),
            Probe("nfail", "number", "How many tests failed?", "2", focus="errors"),
            Probe("npass", "number", "How many tests passed?", str(len(tests) - 2), focus="errors"),
            Probe("status", "exact", f"What status is reported for tests/test_{pm}.py::{pt}?", "PASSED"),
            Probe("absent", "absent", f"What assertion error did tests/test_zeta.py::{ghost} report?", "NOT PRESENT",
                  focus="errors", subject="test_zeta"),
        ]
        out.append(Sample(f"pytest-{seed}-{si}", "pytest_run", "Bash", {"command": "python -m pytest -v"},
                          text, probes))
    return out


# ----------------------------------------------------------------------------- application logs

def app_log_samples(n: int = 4, seed: int = 0) -> list[Sample]:
    out = []
    for si in range(n):
        rng = random.Random(seed * 1000 + 200 + si)
        lines, reqs, errors = [], [], []
        for i in range(260):
            rid = "req_" + _hex(rng, 10)
            ts = f"2026-10-25T10:{i // 60:02d}:{i % 60:02d}.{rng.randint(0, 999):03d}Z"
            path = f"/v1/items/{rng.randint(1, 9999)}"
            if i in (60, 140, 220):
                exc = ["KeyError: 'sku'", "TimeoutError: upstream inventory timed out", "ValueError: bad qty -3"][len(errors)]
                lines.append(f"{ts} ERROR api request_id={rid} path={path} status=500 error=\"{exc}\"")
                lines += ["Traceback (most recent call last):",
                          '  File "/srv/app/handlers.py", line 88, in get_item',
                          "    return inventory.lookup(item_id)",
                          '  File "/srv/app/inventory.py", line 41, in lookup',
                          f"    raise {exc.split(':')[0]}(...)", exc]
                errors.append((rid, exc))
            else:
                lvl = "WARN " if i % 37 == 0 else "INFO "
                lat = rng.randint(3, 900)
                lines.append(f"{ts} {lvl}api request_id={rid} path={path} status=200 latency_ms={lat}")
                reqs.append((rid, path, lat))
        text = "\n".join(lines)
        r1, r2 = rng.sample(reqs, 2)
        ghost = "req_ffffffffff"
        probes = [
            Probe("errid", "exact", f"What is the request_id of the request that failed with \"{errors[1][1]}\"?",
                  errors[1][0], focus="errors"),
            Probe("exc", "exact", f"What exception message was logged for request {errors[0][0]}?", errors[0][1],
                  focus="errors"),
            Probe("nerr", "number", "How many ERROR lines are in the log?", "3", focus="errors"),
            Probe("lat", "number", f"What is the latency_ms of request {r1[0]}?", str(r1[2])),
            Probe("path", "exact", f"Which path did request {r2[0]} hit?", r2[1]),
            Probe("absent", "absent", f"What is the latency_ms of request {ghost}?", "NOT PRESENT", subject=ghost),
        ]
        out.append(Sample(f"log-{seed}-{si}", "app_log", "Bash", {"command": "tail -n 300 /var/log/app.log"},
                          text, probes))
    return out


# ----------------------------------------------------------------------------- repeated command output

def dedup_samples(n: int = 4, seed: int = 0) -> list[Sample]:
    out = []
    for si in range(n):
        rng = random.Random(seed * 1000 + 300 + si)
        owners = ["root", "deploy", "faisal", "www"]
        files = [(f"{rng.choice(WORDS)}_{i}.{rng.choice(['yaml', 'json', 'py', 'md'])}", rng.randint(100, 99999),
                  rng.choice(owners)) for i in range(60)]
        special = files[rng.randrange(60)]
        files = [(n_, s, "root" if o == "audit" else o) for n_, s, o in files]
        idx = files.index(special)
        files[idx] = (special[0], special[1], "audit")
        text = "total 4.2M\n" + "\n".join(f"-rw-r--r--  1 {o:<7} staff {s:>6} Oct 25 10:{i % 60:02d} {n_}"
                                          for i, (n_, s, o) in enumerate(files))
        a = files[rng.randrange(60)]
        probes = [
            Probe("size", "number", f"What is the size in bytes of {a[0]}?", str(a[1])),
            Probe("owner", "exact", "Which file is owned by user audit?", special[0]),
            Probe("count", "number", "How many files are listed (excluding the total line)?", "60"),
            Probe("absent", "absent", "What is the size in bytes of missing_report.txt?", "NOT PRESENT",
                  subject="missing_report.txt"),
        ]
        prior = [{"tool_name": "Bash", "tool_input": {"command": "ls -la configs/"}, "text": text}]
        out.append(Sample(f"dedup-{seed}-{si}", "dedup_pair", "Bash", {"command": "ls -la configs/"}, text, probes,
                          prior=prior))
    return out


# ----------------------------------------------------------------------------- long repeated paths

def bench_path_samples(n: int = 4, seed: int = 0) -> list[Sample]:
    out = []
    for si in range(n):
        rng = random.Random(seed * 1000 + 400 + si)
        prefix = (f"/private/var/folders/05/19_8mk3j7zs8d21kh97f93dm0000gn/T/thrift-bench/"
                  f"edit-{rng.choice(WORDS)}-{rng.choice(WORDS)}__baseline__{si}/rich/")
        mods = rng.sample([f"{w}_{k}" for w in WORDS for k in range(3)], 40)
        lines, defs = [], []
        for m in mods:
            for _ in range(2):
                ln = rng.randint(1, 900)
                name = f"{rng.choice(['def', 'class'])} {m}_{rng.choice(WORDS)}"
                lines.append(f"{prefix}{m}.py:{ln}:{name}(")
                defs.append((f"{prefix}{m}.py", ln, name))
        text = "\n".join(lines)
        f1, f2 = rng.sample(defs, 2)
        probes = [
            Probe("fullpath", "exact", f"What is the full path of the file that contains `{f1[2]}`?", f1[0]),
            Probe("line", "number", f"On which line does `{f2[2]}` appear?", str(f2[1])),
            Probe("absent", "absent", "Which file contains `def nonexistent_fn`?", "NOT PRESENT",
                  subject="nonexistent_fn"),
        ]
        out.append(Sample(f"paths-{seed}-{si}", "bench_paths", "Grep", {"pattern": "^(def|class) ", "path": prefix},
                          text, probes))
    return out


# ----------------------------------------------------------------------------- source files

_DEF = re.compile(r"^(\s*)(?:async\s+)?def\s+(\w+)\(.*\):\s*$")


def _numbered(text: str) -> str:
    return "\n".join(f"{i}\t{ln}" for i, ln in enumerate(text.split("\n"), 1))


def _source_probes(text: str, rng: random.Random, prefix: str) -> list[Probe]:
    lines = text.split("\n")
    defs = [(i + 1, m.group(2), ln.strip()) for i, ln in enumerate(lines) if (m := _DEF.match(ln))]
    top = [d for d in defs if not lines[d[0] - 1].startswith((" ", "\t"))]
    body = [i + 1 for i, ln in enumerate(lines) if ln.startswith("    ") and ln.strip()
            and not _DEF.match(ln) and len(ln.strip()) > 12 and lines.count(ln) == 1]
    probes = []
    if defs:
        d1, d2 = rng.sample(defs, 2) if len(defs) > 1 else (defs[0], defs[0])
        probes.append(Probe(f"{prefix}sig", "exact", f"What is the full signature line (the def line) of function {d1[1]}?",
                            d1[2], focus="signatures"))
        probes.append(Probe(f"{prefix}defline", "number", f"On which line number is function {d2[1]} defined?",
                            str(d2[0]), focus="signatures"))
    probes.append(Probe(f"{prefix}ntop", "number", "How many top-level functions (def at column 0) are defined?",
                        str(len(top)), focus="signatures"))
    if body:
        b = rng.choice(body)
        probes.append(Probe(f"{prefix}bodyline", "exact", f"What is the exact text of line {b} (without leading spaces)?",
                            lines[b - 1].strip(), focus="body"))
    probes.append(Probe(f"{prefix}absent", "absent", "What is the signature of function totally_missing_fn?",
                        "NOT PRESENT", focus="signatures", subject="totally_missing_fn"))
    return probes


def source_samples(files: list, outside_root: str = "/usr/lib/python3.12/site-packages/pkg") -> list[Sample]:
    out = []
    for k, f in enumerate(files):
        f = Path(f)
        text = f.read_text()
        rng = random.Random(f"src-{f.name}")
        out.append(Sample(f"src-{f.stem}", "source_outside", "Read", {"file_path": f"{outside_root}/{f.name}"},
                          _numbered(text), _source_probes(text, rng, "")))
    return out


def reread_samples(files: list, project_root: str = "/work/proj") -> list[Sample]:
    out = []
    for f in files:
        f = Path(f)
        old = f.read_text()
        lines = old.split("\n")
        rng = random.Random(f"reread-{f.name}")
        defs = [i for i, ln in enumerate(lines) if _DEF.match(ln)]
        at = defs[len(defs) // 2] if defs else len(lines) // 2
        new_lines = lines[:at] + ["# reprouter probe: inserted marker line", "SENTINEL_VALUE = 'kiwi-73'", ""] + lines[at:]
        tgt = next((i for i in range(at + 4, len(new_lines)) if new_lines[i].strip().startswith("return")), None)
        if tgt is not None:
            new_lines[tgt] = new_lines[tgt].replace("return", "return  # changed", 1)
        new = "\n".join(new_lines)
        path = f"{project_root}/{f.name}"
        prior = [{"tool_name": "Read", "tool_input": {"file_path": path}, "text": _numbered(old)}]
        probes = [Probe("sentinel", "exact", "What string value is assigned to SENTINEL_VALUE in the latest read?",
                        "kiwi-73"),
                  Probe("sentline", "number", "On which line is SENTINEL_VALUE assigned in the latest read?",
                        str(at + 2))]
        probes += _source_probes(new, rng, "new_")
        out.append(Sample(f"reread-{f.stem}", "reread_pair", "Read", {"file_path": path}, _numbered(new), probes,
                          prior=prior, project_root=project_root))
    return out
