import json
from decimal import Decimal
from pathlib import Path

import httpx
from litellm.llms.custom_httpx.http_handler import HTTPHandler
from test_controller import setup
from test_mini_openrouter import response

from paper_experiments.mini_adapter import MeteredLitellmModel
from paper_experiments.models import mini_model_kwargs


def test_fable_max_effort_and_thinking_survive_the_provider_wire(tmp_path):
    controller, ledger, _, _, _ = setup(tmp_path)
    settings = json.loads((Path(__file__).parents[1] / "configs/fable-models.json").read_text())[
        "claude-fable-5-1"
    ]
    requests = []

    def serve(request):
        assert str(request.url) == "https://openrouter.ai/api/v1/chat/completions"
        requests.append(json.loads(request.content))
        raw = response(cost=0.73).model_dump(mode="json")
        raw["model"] = "anthropic/claude-fable-5.1"
        raw["provider"] = "Anthropic"
        raw["usage"].update(
            cost=0,
            is_byok=True,
            cost_details={"upstream_inference_cost": 0.73},
        )
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
        assert request["model"] == "anthropic/claude-fable-5.1"
        assert "temperature" not in request
        assert request["reasoning"] == {"effort": "max"}
        assert "tool_choice" not in request
        assert request["service_tier"] == "default"
        assert request["provider"] == {
            "only": ["anthropic"],
            "order": ["anthropic"],
            "allow_fallbacks": False,
            "require_parameters": True,
        }
        assert request["tools"][0]["function"]["name"] == "bash"
    assert requests[1]["messages"][1]["reasoning_details"] == first["reasoning_details"]
    assert ledger.charge == Decimal("1.46")
    billed = ledger.snapshot()["attempts"][0]
    assert billed["charge_basis"] == "provider"
    assert billed["response_metadata"]["billing"]["charge_rule"] == "openrouter_plus_upstream"
