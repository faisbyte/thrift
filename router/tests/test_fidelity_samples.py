import json
import re

import pytest

from reprouter.fidelity.samples import (
    app_log_samples,
    bench_path_samples,
    dedup_samples,
    json_record_samples,
    pytest_samples,
    reread_samples,
    source_samples,
)

PY_SOURCE = "\n".join(
    ["import os", "", ""]
    + [line for i in range(12) for line in (
        f"def func_{i}(alpha, beta={i}):",
        f'    """Function number {i}."""',
        f"    total = alpha + beta * {i}",
        "    for k in range(3):",
        "        total += k",
        "    return total",
        "",
        "",
    )]
    + ["class Widget:", "    def __init__(self, size):", "        self.size = size", "",
       "    def grow(self, by: int) -> int:", "        self.size += by", "        return self.size", ""]
)


def check_sample(s):
    assert s.text and s.tool_name and s.probes
    ids = [p.id for p in s.probes]
    assert len(ids) == len(set(ids))
    for p in s.probes:
        assert p.kind in ("exact", "number", "absent")
        assert p.question.strip().endswith("?")
        if p.kind == "exact":
            assert p.answer in s.full_text(), (s.id, p.question, p.answer)
        if p.kind == "absent":
            assert p.subject and p.subject not in s.full_text(), (s.id, p.subject)


@pytest.mark.parametrize("gen", [json_record_samples, pytest_samples, app_log_samples, dedup_samples,
                                 bench_path_samples])
def test_generated_samples_are_answerable_by_construction(gen):
    samples = gen(n=3, seed=7)
    assert len(samples) == 3
    for s in samples:
        check_sample(s)


def test_generation_is_deterministic():
    a = json_record_samples(n=2, seed=1)
    b = json_record_samples(n=2, seed=1)
    assert [s.text for s in a] == [s.text for s in b]
    assert [[(p.question, p.answer) for p in s.probes] for s in a] == [[(p.question, p.answer) for p in s.probes] for s in b]


def test_json_numeric_probe_matches_data():
    s = json_record_samples(n=1, seed=3)[0]
    data = json.loads(s.text)
    nums = [p for p in s.probes if p.kind == "number"]
    assert nums
    for p in nums:
        m = re.search(r"warehouse (\w+)", p.question)
        if m and "total qty" in p.question:
            assert float(p.answer) == sum(r["qty"] for r in data if r["warehouse"] == m.group(1))


def test_probes_are_tagged_by_focus():
    s = app_log_samples(n=1, seed=2)[0]
    assert {p.focus for p in s.probes} >= {"errors", "general"}


def test_source_samples_from_files(tmp_path):
    f = tmp_path / "mod.py"
    f.write_text(PY_SOURCE)
    samples = source_samples([f], outside_root="/usr/lib/python3.12/site-packages/pkg")
    s = samples[0]
    assert s.tool_name == "Read" and s.tool_input["file_path"].startswith("/usr/lib/python3.12/site-packages/pkg/")
    assert s.text.startswith("1\timport os")
    check_sample(s)
    assert any("signature" in p.question for p in s.probes)


def test_reread_samples_have_a_prior_read(tmp_path):
    f = tmp_path / "mod.py"
    f.write_text(PY_SOURCE)
    s = reread_samples([f], project_root="/work/proj")[0]
    assert s.prior and s.prior[0]["tool_name"] == "Read"
    assert s.prior[0]["tool_input"] == s.tool_input
    assert s.prior[0]["text"] != s.text  # the file changed between reads
    check_sample(s)
