from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import Any

import pytest

from minileanagent.models import LiteLLMModel


class FakeResponse:
    def __init__(self, payload: dict[str, Any], cost: float | None = None) -> None:
        self.payload = payload
        self._hidden_params = {} if cost is None else {"response_cost": cost}

    def model_dump(self, mode: str = "json") -> dict[str, Any]:
        assert mode == "json"
        return self.payload


def test_litellm_adapter_normalizes_tool_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    response = FakeResponse(
        {
            "choices": [
                {
                    "message": {
                        "content": "checking",
                        "tool_calls": [
                            {
                                "id": "abc",
                                "function": {
                                    "name": "bash",
                                    "arguments": '{"command": "pwd"}',
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 3},
        },
        cost=0.123,
    )
    fake = SimpleNamespace(
        completion=lambda **kwargs: response,
        completion_cost=lambda completion_response: 9.0,
    )
    monkeypatch.setitem(sys.modules, "litellm", fake)

    model = LiteLLMModel("provider/model", api_base="http://example.test", api_key="secret")
    turn = model.query([{"role": "user", "content": "hi"}], [{"type": "function"}])

    assert turn.content == "checking"
    assert turn.tool_calls[0].name == "bash"
    assert turn.tool_calls[0].arguments == {"command": "pwd"}
    assert turn.cost == 0.123
    assert turn.usage["prompt_tokens"] == 10


def test_litellm_adapter_rejects_bad_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    response = FakeResponse(
        {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {
                                "id": "abc",
                                "function": {"name": "bash", "arguments": "not-json"},
                            }
                        ]
                    }
                }
            ]
        }
    )
    monkeypatch.setitem(
        sys.modules,
        "litellm",
        SimpleNamespace(completion=lambda **kwargs: response, completion_cost=lambda **kwargs: 0),
    )

    with pytest.raises(RuntimeError, match="malformed JSON"):
        LiteLLMModel("provider/model").query([], [])


def test_litellm_adapter_requires_choices(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(
        sys.modules,
        "litellm",
        SimpleNamespace(
            completion=lambda **kwargs: FakeResponse({"choices": []}),
            completion_cost=lambda **kwargs: 0,
        ),
    )

    with pytest.raises(RuntimeError, match="no choices"):
        LiteLLMModel("provider/model").query([], [])


def test_litellm_cost_fallback_and_dict_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    response = FakeResponse(
        {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {
                                "id": "x",
                                "function": {"name": "bash", "arguments": {"command": "true"}},
                            }
                        ]
                    }
                }
            ]
        }
    )
    monkeypatch.setitem(
        sys.modules,
        "litellm",
        SimpleNamespace(
            completion=lambda **kwargs: response,
            completion_cost=lambda completion_response: 0.5,
        ),
    )
    turn = LiteLLMModel("x", max_tokens=100).query([], [])
    assert turn.cost == 0.5
    assert turn.tool_calls[0].arguments == {"command": "true"}


def test_litellm_rejects_non_object_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    response = FakeResponse(
        {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {"id": "x", "function": {"name": "bash", "arguments": "[]"}}
                        ]
                    }
                }
            ]
        }
    )
    monkeypatch.setitem(
        sys.modules,
        "litellm",
        SimpleNamespace(completion=lambda **kwargs: response, completion_cost=lambda **kwargs: 0),
    )
    with pytest.raises(RuntimeError, match="JSON object"):
        LiteLLMModel("x").query([], [])
