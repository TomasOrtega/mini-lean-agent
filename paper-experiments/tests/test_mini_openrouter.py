import json
from copy import deepcopy
from decimal import Decimal
from pathlib import Path

import litellm
import pytest
from test_controller import setup
from test_mini_adapter import make_agent

from paper_experiments.controller import RunStopped
from paper_experiments.ledger import AccountingIncomplete
from paper_experiments.mini_adapter import MeteredLitellmModel, normalize_litellm_usage
from paper_experiments.protocol import GEMINI, OPUS


def response(*, cost=0.012345, usage=True):
    counters = {
        "is_byok": False,
        "prompt_tokens": 100,
        "completion_tokens": 40,
        "prompt_tokens_details": {"cached_tokens": 10},
        "completion_tokens_details": {"reasoning_tokens": 15},
    }
    if cost is not None:
        counters["cost"] = cost
    return litellm.ModelResponse(
        id="gen-paid-response",
        model="google/gemini-3.8-flash",
        provider="Google",
        usage=counters if usage else None,
        choices=[
            {
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": "Check the proof.",
                    "reasoning_details": [
                        {"type": "reasoning.encrypted", "data": "opaque-signature", "index": 0}
                    ],
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {"name": "bash", "arguments": '{"command":"true"}'},
                        }
                    ],
                },
            }
        ],
    )


def test_openrouter_mini_uses_billed_cost_and_preserves_reasoning_across_tools(
    tmp_path, monkeypatch
):
    controller, ledger, _, _, _ = setup(tmp_path)
    model = MeteredLitellmModel(controller, model_name="openrouter/google/gemini-3.8-flash")
    dispatched = []

    def complete(**kwargs):
        dispatched.append(deepcopy(kwargs))
        return response()

    monkeypatch.setattr(litellm, "completion", complete)
    first = model.query([{"role": "user", "content": "Prove the identity."}])
    tool = {"role": "tool", "tool_call_id": "call-1", "content": "ok"}
    model.query([{"role": "user", "content": "Prove the identity."}, first, tool])
    assert ledger.charge == Decimal("0.024690")
    assert first["extra"]["cost"] == 0.012345
    assert dispatched[1]["messages"][1]["reasoning_details"] == first["reasoning_details"]
    assert dispatched[1]["messages"][2] == tool
    assert all(item["num_retries"] == 0 for item in dispatched)
    billed = ledger.snapshot()["attempts"][0]
    assert billed["charge_basis"] == "provider"
    assert billed["response_metadata"]["response_id"] == "gen-paid-response"
    assert billed["response_metadata"]["provider"] == "Google"


def test_openrouter_missing_charge_stops_with_unsettled_accounting(tmp_path, monkeypatch):
    controller, ledger, _, _, _ = setup(tmp_path)
    model = MeteredLitellmModel(controller, model_name="openrouter/google/gemini-3.8-flash")
    monkeypatch.setattr(litellm, "completion", lambda **kwargs: response(cost=None))
    with pytest.raises(AccountingIncomplete):
        model.query([{"role": "user", "content": "Prove the identity."}])
    assert ledger.calls == 1
    assert not ledger.snapshot()["accounting_complete"]


def test_openrouter_opus_retains_reported_reasoning_counters():
    usage = normalize_litellm_usage(response(), OPUS, openrouter=True)
    assert usage.reasoning_reported
    assert usage.billable()["reasoning"] == 15
    assert usage.billable()["output"] == 25


def test_provider_error_archive_redacts_the_host_key(tmp_path, monkeypatch):
    controller, ledger, _, _, _ = setup(tmp_path)
    model = MeteredLitellmModel(controller, model_name="openrouter/google/gemini-3.8-flash")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-private-key")

    def reject(**kwargs):
        raise litellm.AuthenticationError(
            "Rejected Bearer sk-or-test-private-key", model="test", llm_provider="openrouter"
        )

    monkeypatch.setattr(litellm, "completion", reject)
    with pytest.raises(litellm.AuthenticationError):
        model._query([])
    assert "sk-or-test-private-key" not in ledger.path.read_text()


@pytest.mark.parametrize("label", [GEMINI, OPUS])
def test_litellm_openrouter_wire_preserves_reasoning_and_actual_usage(tmp_path, label):
    import httpx
    from litellm.llms.custom_httpx.http_handler import HTTPHandler

    from paper_experiments.models import mini_model_kwargs

    controller, ledger, _, _, _ = setup(tmp_path)
    settings = json.loads((Path(__file__).parents[1] / "configs/models.json").read_text())[label]
    requests = []

    def serve(request):
        assert str(request.url) == "https://openrouter.ai/api/v1/chat/completions"
        requests.append(json.loads(request.content))
        raw = response().model_dump(mode="json")
        raw["model"] = settings["requested_model"]
        if label == OPUS:
            raw["usage"].update(
                cost=0,
                is_byok=True,
                cost_details={
                    "upstream_inference_cost": 0.012345,
                    "upstream_inference_prompt_cost": 0.01,
                    "upstream_inference_completions_cost": 0.002345,
                },
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
        first = model.query([{"role": "user", "content": "Prove the identity."}])
        model.query(
            [
                {"role": "user", "content": "Prove the identity."},
                first,
                {"role": "tool", "tool_call_id": "call-1", "content": "ok"},
            ]
        )
    assert len(requests) == 2
    assert requests[0]["model"] == settings["requested_model"]
    assert requests[0]["reasoning"] == (
        {"effort": "high"} if label == GEMINI else {"max_tokens": 16384, "exclude": False}
    )
    assert requests[0]["provider"] == settings["openrouter_provider"]
    assert requests[1]["messages"][1]["reasoning_details"] == first["reasoning_details"]
    assert ledger.charge == Decimal("0.024690")
    assert ledger.snapshot()["attempts"][0]["response_metadata"]["billing"]["is_byok"] == (
        label == OPUS
    )


def test_litellm_openrouter_has_no_hidden_http_retry(tmp_path):
    import httpx
    from litellm.llms.custom_httpx.http_handler import HTTPHandler

    controller, ledger, _, _, _ = setup(tmp_path)
    requests = []

    def unavailable(request):
        requests.append(request)
        return httpx.Response(500, json={"error": {"message": "unavailable", "code": 500}})

    with httpx.Client(transport=httpx.MockTransport(unavailable)) as client:
        model = MeteredLitellmModel(
            controller,
            model_name="openrouter/google/gemini-3.8-flash",
            model_kwargs={"api_key": "test", "client": HTTPHandler(client=client)},
        )
        with pytest.raises(AccountingIncomplete):
            model.query([{"role": "user", "content": "Prove the identity."}])
    assert len(requests) == ledger.calls == 1
    assert not ledger.snapshot()["accounting_complete"]


def test_upstream_exception_trace_does_not_archive_nested_credentials(tmp_path, monkeypatch):
    controller, ledger, _, candidate, _ = setup(tmp_path)
    model = MeteredLitellmModel(controller, model_name="openrouter/google/gemini-3.8-flash")
    secret = "fake-nested-private-credential"
    monkeypatch.setenv("OPENROUTER_API_KEY", secret)

    def reject(**kwargs):
        try:
            raise ValueError(secret)
        except ValueError as cause:
            raise litellm.AuthenticationError(
                "Rejected request", model="test", llm_provider="openrouter"
            ) from cause

    monkeypatch.setattr(litellm, "completion", reject)
    agent = make_agent(controller, candidate, model)
    with pytest.raises(RunStopped, match="infrastructure_failure"):
        agent.run("Prove the identity.")
    assert secret not in json.dumps(agent.messages)
    assert secret not in json.dumps(controller.state)
    assert secret not in ledger.path.read_text()
    assert agent.messages[-1]["extra"]["exit_status"] == "AuthenticationError"
