import json

import pytest

from reprouter.spend import BudgetExceeded, Ledger


def test_empty_ledger_has_zero_total(tmp_path):
    assert Ledger(tmp_path / "ledger.jsonl", cap_usd=3.0).total() == 0.0


def test_record_appends_entry_and_updates_total(tmp_path):
    led = Ledger(tmp_path / "ledger.jsonl", cap_usd=3.0)
    led.record("probe", "haiku probes batch 1", 0.25)
    led.record("bench", "e2e run", 0.50)
    assert led.total() == pytest.approx(0.75)
    lines = (tmp_path / "ledger.jsonl").read_text().splitlines()
    assert [json.loads(x)["kind"] for x in lines] == ["probe", "bench"]


def test_unbilled_subscription_spend_is_reported_but_not_capped(tmp_path):
    led = Ledger(tmp_path / "ledger.jsonl", cap_usd=1.0)
    led.record("bench", "subscription run", 5.0, billed=False)
    assert led.total() == 0.0
    assert led.unbilled_total() == pytest.approx(5.0)
    led.guard(0.9)  # does not raise: subscription usage is not API spend


def test_guard_refuses_spend_that_would_cross_the_cap(tmp_path):
    led = Ledger(tmp_path / "ledger.jsonl", cap_usd=1.0)
    led.record("probe", "x", 0.8)
    led.guard(0.2)  # exactly at the cap is allowed
    with pytest.raises(BudgetExceeded) as exc:
        led.guard(0.21)
    assert "1.00" in str(exc.value) and "0.80" in str(exc.value)


def test_remaining_never_negative(tmp_path):
    led = Ledger(tmp_path / "ledger.jsonl", cap_usd=1.0)
    led.record("probe", "overshoot", 1.4)
    assert led.remaining() == 0.0


def test_ledger_survives_reopen(tmp_path):
    p = tmp_path / "ledger.jsonl"
    Ledger(p, cap_usd=3.0).record("probe", "a", 0.1)
    assert Ledger(p, cap_usd=3.0).total() == pytest.approx(0.1)


def test_record_rejects_secrets_in_description(tmp_path):
    led = Ledger(tmp_path / "ledger.jsonl", cap_usd=3.0)
    with pytest.raises(ValueError):
        led.record("probe", "used key sk-ant-api03-abcdefghijklmnop", 0.1)
    assert led.total() == 0.0
