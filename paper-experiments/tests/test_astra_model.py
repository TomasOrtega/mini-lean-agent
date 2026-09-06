import json
from decimal import Decimal

import httpx
import pytest
from litellm.llms.custom_httpx.http_handler import HTTPHandler
from test_controller import setup
from test_mini_openrouter import response

from paper_experiments.mini_adapter import MeteredLitellmModel
from paper_experiments.models import mini_model_kwargs, openrouter_reasoning


def astra_settings():
    return {
        "provider": "openrouter",
        "requested_model": "openai/gpt-6-astra",
        "mini_model": "openrouter/openai/gpt-6-astra",
        "thinking": {"level": "max"},
        "max_output_tokens": 128000,
        "request_timeout_seconds": 1800,
        "service_tier": "default",
        "openrouter_provider": {
            "only": ["openai"],
            "order": ["openai"],
            "allow_fallbacks": False,
            "require_parameters": True,
        },
    }


def test_astra_omits_unsupported_temperature_and_keeps_max_reasoning():
    settings = astra_settings()
    kwargs = mini_model_kwargs(settings)
    assert "temperature" not in kwargs
    assert kwargs["max_tokens"] == 128000
    assert kwargs["num_retries"] == 0
    assert kwargs["extra_body"]["reasoning"] == {"effort": "max"}
    assert kwargs["extra_body"]["service_tier"] == "default"
    assert kwargs["extra_body"]["provider"] == settings["openrouter_provider"]


def test_unregistered_reasoning_level_is_rejected():
    settings = astra_settings()
    settings["thinking"] = {"level": "ultra"}
    with pytest.raises(ValueError, match="Unregistered"):
        openrouter_reasoning(settings)


def test_astra_wire_preserves_max_reasoning_tool_state_and_actual_charge(tmp_path):
    controller, ledger, _, _, _ = setup(tmp_path)
    settings = astra_settings()
    requests = []

    def serve(request):
        assert str(request.url) == "https://openrouter.ai/api/v1/chat/completions"
        requests.append(json.loads(request.content))
        raw = response(cost=0.73).model_dump(mode="json")
        raw["model"] = settings["requested_model"]
        raw["provider"] = "OpenAI"
        return httpx.Response(200, json=raw)

    with httpx.Client(transport=httpx.MockTransport(serve)) as client:
        model = MeteredLitellmModel(
            controller,
            model_name=settings["mini_model"],
            model_kwargs={
                **mini_model_kwargs(settings),
                "api_key": "test-not-a-real-key",
                "client": HTTPHandler(client=client),
            },
        )
        user = {"role": "user", "content": "Prove the identity."}
        first = model.query([user])
        model.query([user, first, {"role": "tool", "tool_call_id": "call-1", "content": "ok"}])
    assert len(requests) == 2
    for request in requests:
        assert "temperature" not in request
        assert request["reasoning"] == {"effort": "max"}
        assert request["service_tier"] == "default"
        assert request["provider"] == settings["openrouter_provider"]
        assert request["tools"][0]["function"]["name"] == "bash"
    assert requests[1]["messages"][1]["reasoning_details"] == first["reasoning_details"]
    assert ledger.charge == Decimal("1.46")
    assert ledger.snapshot()["attempts"][0]["charge_basis"] == "provider"
