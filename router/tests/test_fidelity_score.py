import pytest

from reprouter.fidelity.score import is_correct, mcnemar_p, normalize, summarize


def test_normalize_strips_punctuation_quotes_and_case():
    assert normalize("  `Foo.py`. ") == "foo.py"
    assert normalize('"42"') == "42"
    assert normalize("The answer is: 42") == "the answer is: 42"


def test_exact_match_kinds():
    assert is_correct("9d8f9a37", "9d8f9a37", "exact")
    assert not is_correct("9d8f9a3", "9d8f9a37", "exact")
    assert is_correct("Answer: 9d8f9a37", "9d8f9a37", "exact")  # tolerate a short lead-in
    assert not is_correct("9d8f9a37 or 9d8f9a38", "9d8f9a37", "exact")  # hedging two answers is wrong


def test_numeric_match():
    assert is_correct("1,234", "1234", "number")
    assert is_correct("1234.0", "1234", "number")
    assert not is_correct("1235", "1234", "number")
    assert is_correct("The total is 57.5", "57.5", "number")


def test_absent_match():
    assert is_correct("NOT PRESENT", "NOT PRESENT", "absent")
    assert is_correct("not present.", "NOT PRESENT", "absent")
    assert not is_correct("42", "NOT PRESENT", "absent")


def test_mcnemar_exact_binomial():
    assert mcnemar_p(0, 0) == 1.0
    assert mcnemar_p(5, 5) == pytest.approx(1.0)
    assert mcnemar_p(0, 10) < 0.01
    assert mcnemar_p(10, 0) < 0.01


def test_summarize_pairs_only_probes_the_original_got_right():
    rows = [
        {"transform": "t", "probe_type": "exact", "orig_ok": True, "tx_ok": True},
        {"transform": "t", "probe_type": "exact", "orig_ok": True, "tx_ok": False},
        {"transform": "t", "probe_type": "absent", "orig_ok": True, "tx_ok": True},
        {"transform": "t", "probe_type": "exact", "orig_ok": False, "tx_ok": True},
    ]
    s = summarize(rows)["t"]
    assert s["n"] == 4 and s["n_paired"] == 3
    assert s["orig_acc"] == pytest.approx(0.75) and s["tx_acc"] == pytest.approx(0.75)
    assert s["retained"] == pytest.approx(2 / 3)  # of probes the original got right
    assert s["lost"] == 1 and s["gained"] == 1
    assert s["by_type"]["exact"]["retained"] == pytest.approx(0.5)
