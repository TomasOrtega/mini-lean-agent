from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
from langchain_core.messages import HumanMessage
from test_mini_openrouter import response
from test_openrouter import response_payload, router_settings

from paper_experiments.models import provider_charge


@pytest.mark.parametrize(
    ("is_byok", "fee", "upstream", "expected"),
    [
        (True, "0", "0.01161", "0.01161"),
        (True, "0.0005805", "0.01161", "0.0121905"),
        (False, "0.01161", "0.01161", "0.01161"),
    ],
)
def test_actual_charge_includes_upstream_only_for_byok(is_byok, fee, upstream, expected):
    result = SimpleNamespace(
        response_metadata={
            "is_byok": is_byok,
            "cost": fee,
            "cost_details": {"upstream_inference_cost": upstream},
        }
    )
    assert provider_charge(result, openrouter=True) == Decimal(expected)


@pytest.mark.parametrize("is_byok", [None, "true", 1])
def test_missing_or_invalid_byok_flag_cannot_imply_free_inference(is_byok):
    with pytest.raises(ValueError, match="BYOK"):
        provider_charge(
            SimpleNamespace(response_metadata={"cost": 0, "is_byok": is_byok}),
            openrouter=True,
        )


@pytest.mark.parametrize("upstream", [None, -1, True, "NaN", "Infinity"])
def test_byok_requires_complete_valid_upstream_cost(upstream):
    with pytest.raises(ValueError):
        provider_charge(
            SimpleNamespace(
                response_metadata={
                    "cost": 0,
                    "is_byok": True,
                    "cost_details": {"upstream_inference_cost": upstream},
                }
            ),
            openrouter=True,
        )


@pytest.mark.parametrize("harness", ["mini", "axprover"])
async def test_missing_byok_flag_leaves_charge_unknown_and_blocks_dispatch(
    tmp_path, monkeypatch, harness
):
    import litellm
    from test_controller import setup

    from paper_experiments.ax_adapter import _SCOPE, make_client
    from paper_experiments.ledger import AccountingIncomplete
    from paper_experiments.mini_adapter import MeteredLitellmModel

    controller, ledger, _, _, _ = setup(tmp_path)
    if harness == "mini":
        raw = response(cost=0).model_dump(mode="json")
        raw["usage"].pop("is_byok")
        monkeypatch.setattr(litellm, "completion", lambda **kwargs: litellm.ModelResponse(**raw))
        model = MeteredLitellmModel(controller, model_name="openrouter/google/gemini-3.8-flash")
        with pytest.raises(AccountingIncomplete):
            model.query([{"role": "user", "content": "fixture"}])
    else:
        monkeypatch.setenv("OPENROUTER_API_KEY", "fixture-key")
        raw = response_payload()
        raw["usage"].pop("is_byok")

        async def send(self, request, **kwargs):
            return httpx.Response(200, json=raw, request=request)

        monkeypatch.setattr(httpx.AsyncClient, "send", send)
        client = make_client(controller, router_settings(), tmp_path / "requests")
        token = _SCOPE.set({"node": "proposer", "step": 1, "calls": 0})
        try:
            with pytest.raises(RuntimeError, match="BYOK"):
                await client.ainvoke([HumanMessage(content="fixture")])
        finally:
            _SCOPE.reset(token)
    assert ledger.calls == 1
    assert not ledger.snapshot()["accounting_complete"]
    with pytest.raises(AccountingIncomplete):
        ledger.begin_attempt("proposer", controller.state["run"]["model"])


def test_mini_byok_cost_enforces_opus_five_dollar_cap(tmp_path, monkeypatch):
    import litellm
    from test_controller import Clock

    from paper_experiments.controller import RunController, RunStopped
    from paper_experiments.ledger import Ledger, PriceTable
    from paper_experiments.mini_adapter import MeteredLitellmModel
    from paper_experiments.protocol import OPUS, register_runs

    run = next(r for r in register_runs(1)["runs"] if r["model"] == OPUS and r["problem"] == "A1")
    prices = PriceTable(
        "2026-09-04",
        "fixture",
        {OPUS: dict.fromkeys(("input", "output", "reasoning", "cache_read", "cache_write"), "1")},
    )
    ledger = Ledger(tmp_path / "ledger.jsonl", prices)
    controller = RunController(
        run, tmp_path / "run", ledger, lambda p: {"verified": False}, clock=Clock()
    )
    model = MeteredLitellmModel(controller, model_name="openrouter/anthropic/claude-opus-4.5")
    raw = response(cost=0).model_dump(mode="json")
    raw["usage"].update(is_byok=True, cost_details={"upstream_inference_cost": 5.01})
    calls = []

    def complete(**kwargs):
        calls.append(kwargs)
        return litellm.ModelResponse(**raw)

    monkeypatch.setattr(litellm, "completion", complete)
    model.query([{"role": "user", "content": "fixture"}])
    assert ledger.charge == Decimal("5.01")
    billing = ledger.snapshot()["attempts"][0]["response_metadata"]["billing"]
    assert billing["is_byok"] is True
    assert Decimal(billing["openrouter_charge_usd"]) == 0
    assert billing["upstream_inference_cost_usd"] == billing["total_charge_usd"] == "5.01"
    with pytest.raises(RunStopped, match="cost"):
        model._query([])
    assert len(calls) == ledger.calls == 1


async def test_native_ax_byok_wire_metadata_and_crash_replay(tmp_path, monkeypatch):
    from test_controller import setup

    from paper_experiments.ax_adapter import _SCOPE, make_client

    monkeypatch.setenv("OPENROUTER_API_KEY", "fixture-key")
    controller, ledger, _, _, _ = setup(tmp_path)
    client = make_client(controller, router_settings(), tmp_path / "requests")
    raw = response_payload()
    raw["usage"].update(
        cost=0.001,
        is_byok=True,
        cost_details={
            "upstream_inference_cost": 0.02,
            "upstream_inference_prompt_cost": 0.015,
            "upstream_inference_completions_cost": 0.005,
        },
    )
    dispatches = []

    async def send(self, request, **kwargs):
        dispatches.append(request)
        return httpx.Response(200, json=raw, request=request)

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    original_finish = ledger.finish_attempt

    def crash(*args, **kwargs):
        raise KeyboardInterrupt("crash after response journal")

    monkeypatch.setattr(ledger, "finish_attempt", crash)
    token = _SCOPE.set({"node": "proposer", "step": 1, "calls": 0})
    try:
        with pytest.raises(KeyboardInterrupt):
            await client.ainvoke([HumanMessage(content="fixture")])
        monkeypatch.setattr(ledger, "finish_attempt", original_finish)
        for _ in range(2):
            _SCOPE.set({"node": "proposer", "step": 1, "calls": 0})
            result = await client.ainvoke([HumanMessage(content="fixture")])
            assert result.response_metadata["usage"]["is_byok"] is True
    finally:
        _SCOPE.reset(token)
    assert len(dispatches) == ledger.calls == 1
    assert ledger.charge == Decimal("0.021")
    billing = ledger.snapshot()["attempts"][0]["response_metadata"]["billing"]
    assert billing["openrouter_charge_usd"] == "0.001"
    assert billing["upstream_inference_cost_usd"] == "0.02"
    assert billing["total_charge_usd"] == "0.021"
