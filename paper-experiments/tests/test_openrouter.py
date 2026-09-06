import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from langchain_core.messages import HumanMessage, ToolMessage

from paper_experiments.models import ax_llm_config, mini_model_kwargs


def router_settings():
    return {
        "provider": "openrouter",
        "requested_model": "google/gemini-3.8-flash",
        "ax_model": "openrouter:google/gemini-3.8-flash",
        "mini_model": "openrouter/google/gemini-3.8-flash",
        "openrouter_provider": {
            "only": ["google-ai-studio"],
            "order": ["google-ai-studio"],
            "allow_fallbacks": False,
            "require_parameters": True,
        },
        "thinking": {"level": "high"},
        "temperature": 1.0,
        "max_output_tokens": 32768,
        "request_timeout_seconds": 600,
        "max_attempts": 3,
        "context_length": 1048576,
    }


def test_identical_router_reasoning_routes_and_explicit_sdk_retry_disable():
    settings = router_settings()
    ax = ax_llm_config(settings)
    mini = mini_model_kwargs(settings)
    assert ax.provider_config["reasoning"] == mini["extra_body"]["reasoning"] == {"effort": "high"}
    assert ax.provider_config["openrouter_provider"] == mini["extra_body"]["provider"]
    assert ax.provider_config["max_retries"] == mini["num_retries"] == 0
    assert ax.provider_config["model_kwargs"]["retries"] is None
    assert ax.provider_config["timeout"] == mini["timeout"] * 1000
    assert ax.retry_config == {}


@pytest.mark.parametrize("cost", [None, -1, "NaN", "Infinity", True])
def test_router_missing_invalid_charge_cannot_fall_back_to_estimate(cost):
    from paper_experiments.models import provider_charge

    with pytest.raises(ValueError):
        provider_charge(
            SimpleNamespace(response_metadata={"cost": cost, "is_byok": False}), openrouter=True
        )


def test_router_charge_preserves_provider_decimal_and_zero():
    from paper_experiments.models import provider_charge

    for value in ["0.00001234001", "0"]:
        assert provider_charge(
            SimpleNamespace(response_metadata={"cost": value, "is_byok": False}), openrouter=True
        ) == Decimal(value)


def test_router_error_redaction(monkeypatch):
    from paper_experiments.models import redact_provider_error

    secret = "sk-or-v1-unit-test-secret"
    monkeypatch.setenv("OPENROUTER_API_KEY", secret)
    message = redact_provider_error(RuntimeError(f"Authorization: Bearer {secret}; key={secret}"))
    assert secret not in message


def test_default_config_uses_available_models_exact_standard_routes():
    settings = json.loads((Path(__file__).parents[1] / "configs/models.json").read_text())
    for label, model_id, provider in [
        ("gemini-3.8-flash", "google/gemini-3.8-flash", "google-ai-studio"),
        ("claude-opus-4-5", "anthropic/claude-opus-4.5", "anthropic"),
    ]:
        model = settings[label]
        assert model["provider"] == "openrouter"
        assert model["requested_model"] == model_id
        assert model["openrouter_provider"]["only"] == [provider]
        assert model["openrouter_provider"]["allow_fallbacks"] is False


def response_payload():
    return {
        "id": "gen-test-1",
        "model": "google/gemini-3.8-flash",
        "created": 1,
        "object": "chat.completion",
        "provider": "Google AI Studio",
        "system_fingerprint": None,
        "choices": [
            {
                "index": 0,
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": "",
                    "reasoning": "reasoning text",
                    "reasoning_details": [
                        {
                            "type": "reasoning.encrypted",
                            "data": "opaque-signature",
                            "format": "google-gemini-v1",
                            "index": 0,
                        }
                    ],
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {"name": "proof", "arguments": "{}"},
                        }
                    ],
                },
            }
        ],
        "usage": {
            "is_byok": False,
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "total_tokens": 150,
            "prompt_tokens_details": {"cached_tokens": 10, "cache_write_tokens": 5},
            "completion_tokens_details": {"reasoning_tokens": 40},
            "cost": 0.00012345,
        },
    }


def test_real_chatopenrouter_wire_roundtrip_preserves_signature_usage_and_cost(monkeypatch):
    from paper_experiments.models import langchain_usage, openrouter_model, provider_charge

    monkeypatch.setenv("OPENROUTER_API_KEY", "unit-test-key")
    requests = []

    def send(self, request, **kwargs):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=response_payload(), request=request)

    monkeypatch.setattr(httpx.Client, "send", send)
    model = openrouter_model(router_settings())
    first = model.invoke([HumanMessage(content="test")])
    model.invoke(
        [HumanMessage(content="test"), first, ToolMessage(content="ok", tool_call_id="call-1")]
    )
    assert (
        requests[1]["messages"][1]["reasoning_details"]
        == response_payload()["choices"][0]["message"]["reasoning_details"]
    )
    assert requests[0]["provider"] == router_settings()["openrouter_provider"]
    assert requests[0]["reasoning"] == {"effort": "high"}
    assert requests[0]["max_tokens"] == 32768
    assert "retries" not in requests[0]
    assert provider_charge(first, openrouter=True) == Decimal("0.00012345")
    assert langchain_usage(first).billable() == {
        "input": 85,
        "output": 10,
        "reasoning": 40,
        "cache_read": 10,
        "cache_write": 5,
    }


def test_real_chatopenrouter_rejected_call_has_no_sdk_retry(monkeypatch):
    from ax_prover.utils.llm import create_llm

    monkeypatch.setenv("OPENROUTER_API_KEY", "unit-test-key")
    attempts = []

    def send(self, request, **kwargs):
        attempts.append(request)
        if len(attempts) > 1:
            raise AssertionError("Hidden SDK retry")
        return httpx.Response(
            500, json={"error": {"message": "unit test", "code": 500}}, request=request
        )

    monkeypatch.setattr(httpx.Client, "send", send)
    model = create_llm(ax_llm_config(router_settings()))
    with pytest.raises(Exception, match="unit test"):
        model.invoke([HumanMessage(content="test")])
    assert len(attempts) == 1


async def test_ax_router_schema_billing_and_crash_replay(tmp_path, monkeypatch):
    from pydantic import BaseModel
    from test_controller import setup

    from paper_experiments.ax_adapter import _SCOPE, make_client
    from paper_experiments.models import openrouter_model

    monkeypatch.setenv("OPENROUTER_API_KEY", "unit-test-key")
    controller, ledger, _, _, _ = setup(tmp_path)
    client = make_client(controller, router_settings(), tmp_path / "requests")
    model = openrouter_model(router_settings())
    response = model._create_chat_result(response_payload()).generations[0].message

    class Answer(BaseModel):
        proof: str

    assert client._structured_output_bind_kwargs(Answer)["response_format"] == {
        "type": "json_schema",
        "json_schema": {
            "name": "Answer",
            "schema": Answer.model_json_schema(),
            "strict": True,
        },
    }
    calls = []

    async def fake(self, messages, **kwargs):
        calls.append(messages)
        return response

    monkeypatch.setattr("ax_prover.utils.llm.LLMClient.ainvoke", fake)
    original_finish = ledger.finish_attempt

    def crashed(*args, **kwargs):
        raise KeyboardInterrupt("crash after persisted response")

    monkeypatch.setattr(ledger, "finish_attempt", crashed)
    token = _SCOPE.set({"node": "proposer", "step": 1, "calls": 0})
    try:
        with pytest.raises(KeyboardInterrupt):
            await client.ainvoke([HumanMessage(content="test")])
        monkeypatch.setattr(ledger, "finish_attempt", original_finish)
        for _ in range(2):
            _SCOPE.set({"node": "proposer", "step": 1, "calls": 0})
            assert await client.ainvoke([HumanMessage(content="test")]) == response
    finally:
        _SCOPE.reset(token)
    assert len(calls) == ledger.calls == 1
    assert ledger.charge == Decimal("0.00012345")
    assert ledger.snapshot()["attempts"][0]["charge_basis"] == "provider"


@pytest.mark.parametrize("schema_name", ["ProverResult", "ReviewDecision"])
async def test_native_ax_tools_and_output_schema_reach_router_wire(
    tmp_path, monkeypatch, schema_name
):
    from ax_prover.models import proving
    from ax_prover.tools.lean_search import SearchLeanSearchConfig, create_search_lean_search_tool
    from test_controller import setup

    from paper_experiments.ax_adapter import _SCOPE, make_client

    monkeypatch.setenv("OPENROUTER_API_KEY", "unit-test-key")
    controller, ledger, _, _, _ = setup(tmp_path)
    client = make_client(controller, router_settings(), tmp_path / "requests")
    tool = await create_search_lean_search_tool(
        SearchLeanSearchConfig(), SimpleNamespace(get_tool_resources=lambda name: None)
    )
    requests = []

    async def send(self, request, **kwargs):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=response_payload(), request=request)

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    schema = getattr(proving, schema_name)
    token = _SCOPE.set({"node": "proposer", "step": 1, "calls": 0})
    try:
        await client.ainvoke([HumanMessage(content="test")], tools=[tool], output_schema=schema)
    finally:
        _SCOPE.reset(token)
    assert requests[0]["tools"][0]["function"]["name"] == "search_lean_search_tool"
    assert requests[0]["tools"][0]["function"]["parameters"] == {
        "type": "object",
        "required": ["query"],
        "properties": {"query": {"type": "string", "description": "Search query string"}},
    }
    assert (
        requests[0]["response_format"]
        == client._structured_output_bind_kwargs(schema)["response_format"]
    )
    assert ledger.calls == 1
    assert ledger.charge == Decimal("0.00012345")
