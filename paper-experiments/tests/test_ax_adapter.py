import json
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import BaseModel

from paper_experiments.ax_adapter import _SCOPE, make_client
from paper_experiments.controller import RunController
from paper_experiments.ledger import Ledger, PriceTable
from paper_experiments.protocol import register_runs


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "test-not-a-real-key")
    settings = {
        "provider": "google",
        "ax_model": "google_genai:gemini-3.8-flash",
        "requested_model": "gemini-3.8-flash",
        "thinking": {"level": "high"},
        "temperature": 1,
        "max_output_tokens": 32768,
        "request_timeout_seconds": 600,
        "max_attempts": 3,
    }
    run = next(r for r in register_runs(0)["runs"] if r["harness"] == "axprover")
    prices = PriceTable(
        "2026-09-04",
        "test",
        {
            run["model"]: {
                "input": "1",
                "output": "1",
                "reasoning": "1",
                "cache_read": "1",
                "cache_write": "1",
            }
        },
    )
    ledger = Ledger(tmp_path / "ledger.jsonl", prices)
    controller = RunController(run, tmp_path, ledger, lambda p: {"verified": False})
    return make_client(controller, settings, tmp_path / "requests"), controller


def test_google_structured_schema_regression(setup):
    client, _ = setup

    class Answer(BaseModel):
        proof: str

    assert (
        client._structured_output_bind_kwargs(Answer)["response_json_schema"]
        == Answer.model_json_schema()
    )


@pytest.mark.parametrize("problem,call_cap", [("A1", 200), ("A5", 300), ("B6", 300)])
async def test_native_iteration_limit_cannot_preempt_registered_api_limit(
    setup, monkeypatch, tmp_path, problem, call_cap
):
    from paper_experiments.ax_adapter import run_axprover

    _, original = setup
    run = next(
        run
        for run in register_runs(0)["runs"]
        if run["harness"] == "axprover" and run["problem"] == problem
    )
    controller = RunController(run, tmp_path / problem, original.ledger, original.evaluator)
    settings = {
        "provider": "google",
        "ax_model": "google_genai:gemini-3.8-flash",
        "requested_model": "gemini-3.8-flash",
        "thinking": {"level": "high"},
        "temperature": 1,
        "max_output_tokens": 32768,
        "request_timeout_seconds": 600,
        "max_attempts": 3,
    }

    class ConfigurationObserved(Exception):
        pass

    def config(**kwargs):
        assert kwargs["max_iterations"] > call_cap
        assert kwargs["proposer_tools"] == {
            "search_lean": {
                "tool_type": "search_lean_search",
                "server_url": "http://127.0.0.1:8765",
            }
        }
        raise ConfigurationObserved

    monkeypatch.setattr("ax_prover.config.ProverConfig", config)
    with pytest.raises(ConfigurationObserved):
        await run_axprover(
            controller,
            workspace=tmp_path,
            target=Path("Target.lean"),
            declaration="smoke",
            task="Prove it",
            settings=settings,
            image="sha256:" + "a" * 64,
            retrieval={
                "tool_config": {
                    "tool_type": "search_lean_search",
                    "server_url": "http://127.0.0.1:8765",
                }
            },
        )


async def test_provider_response_replay_preserves_signatures_without_rebilling(setup, monkeypatch):
    client, controller = setup
    calls = []

    async def fake(self, messages, **kwargs):
        calls.append(messages)
        return AIMessage(
            content="proof",
            additional_kwargs={"thought_signature": "opaque-provider-state"},
            usage_metadata={
                "input_tokens": 100,
                "output_tokens": 50,
                "total_tokens": 150,
                "output_token_details": {"reasoning": 40},
            },
        )

    monkeypatch.setattr("ax_prover.utils.llm.LLMClient.ainvoke", fake)
    messages = [HumanMessage(content="prove the statement")]
    token = _SCOPE.set({"node": "proposer", "step": 1, "calls": 0})
    try:
        first = await client.ainvoke(messages)
        _SCOPE.set({"node": "proposer", "step": 1, "calls": 0})
        replay = await client.ainvoke(messages)
    finally:
        _SCOPE.reset(token)
    assert replay.additional_kwargs == first.additional_kwargs
    assert len(calls) == controller.ledger.calls == 1
    records = list((controller.output_dir / "requests").glob("*.json"))
    assert json.loads(records[0].read_text())["request"]["messages"]


async def test_replay_rejects_changed_prompt(setup, monkeypatch):
    client, _ = setup

    async def fake(self, messages, **kwargs):
        return AIMessage(
            content="proof",
            usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        )

    monkeypatch.setattr("ax_prover.utils.llm.LLMClient.ainvoke", fake)
    token = _SCOPE.set({"node": "memory_processor", "step": 2, "calls": 0})
    try:
        await client.ainvoke([HumanMessage(content="original")])
        _SCOPE.set({"node": "memory_processor", "step": 2, "calls": 0})
        with pytest.raises(RuntimeError, match="changed"):
            await client.ainvoke([HumanMessage(content="new hint")])
    finally:
        _SCOPE.reset(token)


async def test_unknown_provider_failure_blocks_further_spend(setup, monkeypatch):
    client, controller = setup

    async def fake(self, messages, **kwargs):
        raise TimeoutError("no billing confirmation")

    monkeypatch.setattr("ax_prover.utils.llm.LLMClient.ainvoke", fake)
    token = _SCOPE.set({"node": "reviewer", "step": 3, "calls": 0})
    try:
        with pytest.raises(TimeoutError):
            await client.ainvoke([HumanMessage(content="review")])
    finally:
        _SCOPE.reset(token)
    assert controller.ledger.snapshot()["charge_usd"] is None
    assert controller.ledger.calls == 1


async def test_resumed_request_uses_remaining_retry_allowance(setup, monkeypatch):
    from paper_experiments.ax_adapter import request_record
    from paper_experiments.protocol import content_hash

    client, controller = setup
    messages = [HumanMessage(content="same request")]
    metadata = {
        "requested_model": "gemini-3.8-flash",
        "request_sha256": content_hash(request_record(messages, None, None)),
        "graph_step": 8,
        "ordinal": 0,
    }
    for _ in range(2):
        attempt = controller.ledger.begin_attempt(
            "proposer", controller.state["run"]["model"], metadata
        )
        controller.ledger.finish_attempt(attempt, None, actual_charge="0", error="rate limit")

    class RateLimit(Exception):
        code = 429

    async def reject(*args, **kwargs):
        raise RateLimit()

    async def no_wait(*args):
        pass

    monkeypatch.setattr("ax_prover.utils.llm.LLMClient.ainvoke", reject)
    monkeypatch.setattr("paper_experiments.ax_adapter.asyncio.sleep", no_wait)
    token = _SCOPE.set({"node": "proposer", "step": 8, "calls": 0})
    try:
        with pytest.raises(RateLimit):
            await client.ainvoke(messages)
    finally:
        _SCOPE.reset(token)
    assert controller.ledger.calls == 3


@pytest.mark.parametrize("missing", ["latest-candidate.lean", "graph.sqlite"])
async def test_resume_rejects_missing_native_ax_state(setup, missing):
    from paper_experiments.ax_adapter import run_axprover

    _, controller = setup
    if missing == "graph.sqlite":
        (controller.output_dir / "latest-candidate.lean").write_text("saved proof")
    with pytest.raises(ValueError, match=missing):
        await run_axprover(
            controller,
            workspace=controller.output_dir,
            target=controller.output_dir / "Target.lean",
            declaration="target",
            task="same task",
            settings={},
            image="sha256:" + "a" * 64,
            retrieval={},
            resume_state={"graph_started": True},
        )


async def test_resume_rejects_missing_native_search_record_manifest(setup):
    from paper_experiments.ax_adapter import run_axprover

    _, controller = setup
    for filename in ("latest-candidate.lean", "graph.sqlite"):
        (controller.output_dir / filename).write_text("saved state")
    with pytest.raises(ValueError, match="search_records"):
        await run_axprover(
            controller,
            workspace=controller.output_dir,
            target=Path("Target.lean"),
            declaration="target",
            task="same task",
            settings={},
            image="sha256:" + "a" * 64,
            retrieval={},
            resume_state={"graph_started": True},
        )


async def test_dispatch_checkpoint_preserves_latest_search_record_manifest(setup, monkeypatch):
    _, controller = setup
    settings = {
        "provider": "google",
        "ax_model": "google_genai:gemini-3.8-flash",
        "requested_model": "gemini-3.8-flash",
        "thinking": {"level": "high"},
        "temperature": 1,
        "max_output_tokens": 32768,
        "request_timeout_seconds": 600,
        "max_attempts": 3,
    }
    driver = {"graph_started": True, "search_records": {"a" * 64: "b" * 64}}
    client = make_client(
        controller, settings, controller.output_dir / "requests", driver_state=driver
    )

    async def provider(*args, **kwargs):
        saved = json.loads(controller.path.read_text())
        assert saved["driver_state"] == driver
        assert saved["ledger"]["calls"] == 1
        return AIMessage(
            content="proof",
            usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        )

    monkeypatch.setattr("ax_prover.utils.llm.LLMClient.ainvoke", provider)
    token = _SCOPE.set({"node": "proposer", "step": 1, "calls": 0})
    try:
        await client.ainvoke([HumanMessage(content="continue after the search")])
    finally:
        _SCOPE.reset(token)
