import json
from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace

import pytest
from minisweagent.exceptions import FormatError
from test_controller import request, setup

from paper_experiments.controller import RunStopped
from paper_experiments.mini_adapter import (
    MeteredLitellmModel,
    SnapshotAgent,
    normalize_litellm_usage,
)
from paper_experiments.protocol import GEMINI, OPUS


class Environment:
    container_id = "durabletestcontainer"

    def execute(self, action):
        return {"output": "lean failed", "returncode": 1}

    def get_template_vars(self):
        return {}

    def serialize(self):
        return {}


class ScriptedModel:
    def __init__(self, controller, ledger, *, bad_format=False, candidate=None):
        self.controller, self.ledger = controller, ledger
        self.bad_format, self.candidate = bad_format, candidate
        self.seen = []

    def query(self, messages):
        self.seen.append(deepcopy(messages))
        request(self.controller, self.ledger, "2")
        response = {
            "role": "assistant",
            "content": "thinking",
            "reasoning_blocks": [{"signature": "provider-signature", "data": "reasoning-state"}],
            "extra": {"cost": 2, "actions": [{"command": "lake env lean Target.lean"}]},
        }
        if self.bad_format:
            raise FormatError(response, {"role": "user", "content": "Use the bash schema"})
        if self.candidate:
            self.candidate.write_text("valid")
        return response

    def format_observation_messages(self, message, outputs, template_vars):
        return [{"role": "tool", "content": outputs[0]["output"], "tool_call_id": "id1"}]

    def format_message(self, **kwargs):
        return kwargs

    def get_template_vars(self):
        return {}

    def serialize(self):
        return {}


def make_agent(controller, candidate, model, *, resume=None):
    return SnapshotAgent(
        model,
        Environment(),
        controller=controller,
        candidate=candidate,
        driver_config={"workspace": str(candidate.parent)},
        resume_state=resume,
        system_template="system",
        instance_template="{{ task }}",
        cost_limit=0,
        step_limit=0,
        wall_time_limit_seconds=0,
    )


def test_upstream_format_error_messages_and_reasoning_survive_continuation(tmp_path):
    controller, ledger, _, candidate, _ = setup(tmp_path)
    first = ScriptedModel(controller, ledger, bad_format=True)
    agent = make_agent(controller, candidate, first)
    with pytest.raises(RunStopped, match="cost"):
        agent.run("original task")
    frozen = controller.resume_extended()
    assert frozen["consecutive_format_errors"] == 1
    assert [m["role"] for m in frozen["messages"]] == ["system", "user", "assistant", "user"]
    assert frozen["messages"][2]["reasoning_blocks"][0]["signature"] == "provider-signature"
    second = ScriptedModel(controller, ledger, candidate=candidate)
    resumed = make_agent(controller, candidate, second, resume=frozen)
    with pytest.raises(RunStopped, match="verified"):
        resumed.run("original task")
    assert second.seen == [frozen["messages"]]
    assert sum(m.get("content") == "original task" for m in resumed.messages) == 1
    assert ledger.calls == 2
    assert ledger.charge == Decimal("4")
    assert controller.state["low_endpoint"]["verified"] is False


def test_changed_initial_prompt_is_rejected_before_inference(tmp_path):
    controller, ledger, _, candidate, _ = setup(tmp_path)
    first = ScriptedModel(controller, ledger)
    agent = make_agent(controller, candidate, first)
    with pytest.raises(RunStopped):
        agent.run("original task")
    frozen = controller.resume_extended()
    second = ScriptedModel(controller, ledger)
    with pytest.raises(ValueError, match="initial system or task"):
        make_agent(controller, candidate, second, resume=frozen).run("new hint")
    assert second.seen == []


def test_usage_includes_gemini_thinking_without_inventing_anthropic_split():
    raw = {
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 40,
            "prompt_tokens_details": {"cached_tokens": 10},
            "completion_tokens_details": {"reasoning_tokens": 15},
        }
    }
    response = SimpleNamespace(model_dump=lambda **kwargs: raw)
    gemini = normalize_litellm_usage(response, GEMINI)
    assert gemini.billable() == {
        "input": 90,
        "output": 25,
        "reasoning": 15,
        "cache_read": 10,
        "cache_write": 0,
    }
    opus = normalize_litellm_usage(response, OPUS)
    assert opus.billable()["output"] == 40
    assert not opus.reasoning_reported


def test_each_retry_is_reserved_before_provider_dispatch(tmp_path, monkeypatch):
    import litellm

    controller, ledger, _, _, _ = setup(tmp_path)
    model = MeteredLitellmModel(controller, model_name="gemini/gemini-3.8-flash")
    observed_calls = []

    def failed_provider(**kwargs):
        observed_calls.append(ledger.calls)
        durable = json.loads(controller.path.read_text())["ledger"]
        assert durable["calls"] == ledger.calls
        assert not durable["accounting_complete"]
        assert kwargs["num_retries"] == 0
        raise litellm.RateLimitError("rejected", model="gemini-3.8-flash", llm_provider="gemini")

    monkeypatch.setattr(litellm, "completion", failed_provider)
    for _ in range(2):
        with pytest.raises(litellm.RateLimitError):
            model._query([])
    assert observed_calls == [1, 2]
    assert ledger.calls == 2
    assert ledger.charge == 0


def test_local_digest_image_is_accepted_without_a_registry_reference(tmp_path, monkeypatch):
    import paper_experiments.mini_adapter as adapter

    controller, _, _, candidate, _ = setup(tmp_path)
    environment = SimpleNamespace(container_id="durable", received=None)

    def make_environment(**kwargs):
        environment.received = kwargs
        return environment

    class Agent:
        def __init__(self, *args, **kwargs):
            self.extra_template_vars = {}

        def run(self, task):
            return {}

    monkeypatch.setattr(adapter, "DurableDockerEnvironment", make_environment)
    monkeypatch.setattr(adapter, "SnapshotAgent", Agent)
    adapter.run_mini(
        controller,
        workspace=tmp_path,
        target=candidate,
        declaration="example",
        task="original",
        model_config={"model_name": "gemini/gemini-3.8-flash"},
        image="sha256:" + "a" * 64,
    )
    assert environment.received["image"] == "sha256:" + "a" * 64


def test_request_limit_freezes_latest_real_messages_without_synthetic_exit(tmp_path):
    controller, ledger, _, candidate, _ = setup(tmp_path)
    for _ in range(300):
        attempt = ledger.begin_attempt("proposer", GEMINI)
        ledger.finish_attempt(attempt, None, actual_charge="0")
    model = MeteredLitellmModel(controller, model_name="gemini/gemini-3.8-flash")
    agent = make_agent(controller, candidate, model)
    with pytest.raises(RunStopped):
        agent.run("original task")
    assert len(controller.state["snapshots"]) == 1
    frozen = controller.resume_extended()
    assert [m["role"] for m in frozen["messages"]] == ["system", "user"]
    assert frozen["agent_calls"] == 1
    assert ledger.calls == 300


def test_continuation_preserves_remaining_retry_allowance(tmp_path, monkeypatch):
    import litellm

    controller, ledger, _, _, _ = setup(tmp_path)
    for _ in range(2):
        attempt = ledger.begin_attempt("proposer", GEMINI)
        ledger.finish_attempt(attempt, None, actual_charge="0")
    model = MeteredLitellmModel(controller, model_name="gemini/gemini-3.8-flash")
    model.query_pending = True
    model.attempts_in_query = 2
    monkeypatch.setenv("MSWEA_MODEL_RETRY_STOP_AFTER_ATTEMPT", "3")

    def reject(**kwargs):
        raise litellm.RateLimitError("rejected", model="gemini-3.8-flash", llm_provider="gemini")

    monkeypatch.setattr(litellm, "completion", reject)
    with pytest.raises(litellm.RateLimitError):
        model.query([])
    assert ledger.calls == 3
    assert not model.query_pending


def test_default_zero_usage_does_not_make_a_missing_provider_report_free():
    response = SimpleNamespace(
        model_dump=lambda **kwargs: {"usage": {"prompt_tokens": 0, "completion_tokens": 0}}
    )
    assert normalize_litellm_usage(response, GEMINI) is None
