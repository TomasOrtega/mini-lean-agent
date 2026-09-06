import asyncio
import json
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage, message_to_dict
from test_controller import setup

from paper_experiments.ax_adapter import _SCOPE, make_client, request_record
from paper_experiments.controller import atomic_json
from paper_experiments.models import langchain_usage
from paper_experiments.protocol import GEMINI, content_hash


def replay_fixture(tmp_path, monkeypatch):
    from ax_prover.utils.llm import LLMClient

    monkeypatch.setattr(LLMClient, "__init__", lambda self, config: None)
    controller, ledger, _, _, _ = setup(tmp_path)
    settings = json.loads((Path(__file__).parents[1] / "configs" / "models.json").read_text())[
        GEMINI
    ]
    settings.update(provider="google", requested_model=GEMINI, ax_model=f"google_genai:{GEMINI}")
    client = make_client(controller, settings, tmp_path / "requests")
    messages = [HumanMessage(content="original task")]
    request = request_record(messages, None, None)
    request_hash = content_hash(request)
    attempt_id = ledger.begin_attempt(
        "proposer",
        GEMINI,
        {
            "requested_model": GEMINI,
            "request_sha256": request_hash,
            "graph_step": 1,
            "ordinal": 0,
        },
    )
    response = AIMessage(
        content="proof",
        additional_kwargs={"reasoning_signature": "opaque"},
        usage_metadata={
            "input_tokens": 100,
            "output_tokens": 40,
            "total_tokens": 140,
            "input_token_details": {"cache_read": 10},
            "output_token_details": {"reasoning": 15},
        },
    )
    raw = message_to_dict(response)
    path = tmp_path / "requests" / "000001-proposer-0000.json"
    saved = {
        "request": request,
        "request_sha256": request_hash,
        "attempt_id": attempt_id,
        "response": raw,
        "response_sha256": content_hash(raw),
    }
    atomic_json(path, saved)
    return client, ledger, messages, response, path, saved


def invoke(client, messages):
    token = _SCOPE.set({"node": "proposer", "step": 1, "calls": 0})
    try:
        return asyncio.run(client.ainvoke(messages))
    finally:
        _SCOPE.reset(token)


def test_crash_after_response_journal_settles_original_attempt_once(tmp_path, monkeypatch):
    client, ledger, messages, response, _, _ = replay_fixture(tmp_path, monkeypatch)
    assert ledger.snapshot()["accounting_complete"] is False
    assert invoke(client, messages) == response
    assert ledger.charge == Decimal("0.00014")
    assert ledger.calls == 1
    assert ledger.snapshot()["attempts"][0]["usage"] == asdict(langchain_usage(response))
    before = ledger.path.read_bytes()
    assert invoke(client, messages) == response
    assert ledger.path.read_bytes() == before


@pytest.mark.parametrize("change", ["response", "request", "attempt", "model"])
def test_replay_rejects_mismatched_response_attempt_or_model(tmp_path, monkeypatch, change):
    client, ledger, messages, _, path, saved = replay_fixture(tmp_path, monkeypatch)
    if change == "response":
        saved["response"]["data"]["content"] = "changed"
    elif change == "request":
        saved["request"]["messages"] = []
    elif change == "attempt":
        saved["attempt_id"] = 99
    else:
        events = [json.loads(line) for line in ledger.path.read_text().splitlines()]
        events[1]["metadata"]["requested_model"] = "unregistered-other-model"
        ledger.path.write_text("\n".join(json.dumps(event) for event in events) + "\n")
    atomic_json(path, saved)
    with pytest.raises(RuntimeError):
        invoke(client, messages)
