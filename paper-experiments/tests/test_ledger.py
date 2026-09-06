from decimal import Decimal

import pytest

from paper_experiments.ledger import AccountingIncomplete, Ledger, PriceTable, Usage


def prices():
    return PriceTable(
        "2026-09-04",
        "Test fixture, not production prices",
        {
            "model": {
                "input": "1",
                "output": "2",
                "reasoning": "3",
                "cache_read": "0.1",
                "cache_write": "1.5",
            }
        },
    )


def test_disjoint_token_accounting_does_not_double_count_reasoning_or_cache(tmp_path):
    ledger = Ledger(tmp_path / "usage.jsonl", prices())
    identifier = ledger.begin_attempt("review", "model")
    assert ledger.calls == 1
    with pytest.raises(AccountingIncomplete):
        ledger.begin_attempt("proposer", "model")
    usage = Usage(1_000_000, 1_000_000, 100_000, 100_000, 100_000, True, True)
    ledger.finish_attempt(identifier, usage)
    assert ledger.charge == Decimal("3.06")
    assert ledger.snapshot()["tokens"]["output"] == 900_000
    assert Ledger(tmp_path / "usage.jsonl", prices()).charge == Decimal("3.06")
    with pytest.raises(ValueError, match="twice"):
        ledger.finish_attempt(identifier, usage)


def test_all_attempts_count_and_missing_billing_fails_closed(tmp_path):
    ledger = Ledger(tmp_path / "usage.jsonl", prices())
    first = ledger.begin_attempt("memory", "model")
    ledger.finish_attempt(first, None, error="timeout: provider may have billed")
    assert ledger.snapshot()["charge_usd"] is None
    assert ledger.snapshot()["known_charge_usd"] == "0"
    with pytest.raises(AccountingIncomplete):
        ledger.begin_attempt("memory", "model")
    ledger.finish_attempt(first, None, actual_charge="0.25", metadata={"invoice": "test"})
    second = ledger.begin_attempt("memory", "model")
    ledger.finish_attempt(second, None, actual_charge="0", error="explicitly unbilled")
    assert ledger.calls == 2
    assert ledger.charge == Decimal("0.25")
    assert not ledger.snapshot()["usage_complete"]


def test_invalid_usage_unregistered_models_and_price_changes_are_rejected(tmp_path):
    ledger = Ledger(tmp_path / "usage.jsonl", prices())
    with pytest.raises(ValueError, match="Unregistered"):
        ledger.begin_attempt("embedding", "hidden-model")
    with pytest.raises(ValueError, match="contradict"):
        Usage(1, 1, 2, 0, 0, True, True).billable()
    changed = prices()
    changed.models["model"]["input"] = "2"
    with pytest.raises(ValueError, match="frozen price"):
        Ledger(tmp_path / "usage.jsonl", changed)
