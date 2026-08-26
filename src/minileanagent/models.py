"""Model adapters.

LiteLLM is intentionally kept behind one tiny adapter so experiments can swap it
without changing the agent or trajectory format.
"""

from __future__ import annotations

import json
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

from minileanagent.types import JsonDict, Message, ModelTurn, ToolCall, ToolSchema


def _to_dict(value: Any) -> JsonDict:
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        dumped = value.model_dump(mode="json")
        if isinstance(dumped, dict):
            return dumped
    if hasattr(value, "dict"):
        dumped = value.dict()
        if isinstance(dumped, dict):
            return dumped
    raise TypeError(f"Could not normalize object of type {type(value)!r}")


@dataclass
class LiteLLMModel:
    """Use any provider supported by LiteLLM or OpenRouter."""

    model_name: str
    api_base: str | None = None
    api_key: str | None = None
    temperature: float = 0.0
    max_tokens: int | None = None
    extra_kwargs: JsonDict = field(default_factory=dict)

    @property
    def name(self) -> str:
        return self.model_name

    def query(self, messages: list[Message], tools: list[ToolSchema]) -> ModelTurn:
        try:
            import litellm
        except ImportError as exc:  # pragma: no cover - dependency is required in normal installs
            raise RuntimeError("Install mini-lean-agent with its default dependencies") from exc

        kwargs: JsonDict = {
            "model": self.model_name,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
            "temperature": self.temperature,
            "drop_params": True,
            **self.extra_kwargs,
        }
        if self.api_base:
            kwargs["api_base"] = self.api_base
        if self.api_key:
            kwargs["api_key"] = self.api_key
        if self.max_tokens is not None:
            kwargs["max_tokens"] = self.max_tokens

        response = litellm.completion(**kwargs)
        data = _to_dict(response)
        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError("Model returned no choices")

        choice = choices[0]
        message = choice.get("message") or {}
        calls: list[ToolCall] = []
        for index, raw_call in enumerate(message.get("tool_calls") or []):
            function = raw_call.get("function") or {}
            raw_arguments = function.get("arguments") or "{}"
            if isinstance(raw_arguments, str):
                try:
                    arguments = json.loads(raw_arguments)
                except json.JSONDecodeError as exc:
                    raise RuntimeError(
                        f"Model produced malformed JSON arguments for {function.get('name')}: "
                        f"{raw_arguments!r}"
                    ) from exc
            elif isinstance(raw_arguments, dict):
                arguments = raw_arguments
            else:
                raise RuntimeError(f"Unexpected tool argument type: {type(raw_arguments)!r}")
            if not isinstance(arguments, dict):
                raise RuntimeError("Tool arguments must decode to a JSON object")
            calls.append(
                ToolCall(
                    id=str(raw_call.get("id") or f"call_{index}"),
                    name=str(function.get("name") or ""),
                    arguments=arguments,
                )
            )

        cost = 0.0
        hidden = getattr(response, "_hidden_params", None)
        if isinstance(hidden, dict) and hidden.get("response_cost") is not None:
            cost = float(hidden["response_cost"])
        else:
            with suppress(Exception):
                value = litellm.completion_cost(completion_response=response)
                if value is not None:
                    cost = float(value)

        usage = data.get("usage") or {}
        return ModelTurn(
            content=str(message.get("content") or ""),
            tool_calls=tuple(calls),
            cost=cost,
            usage=usage if isinstance(usage, dict) else {},
            finish_reason=choice.get("finish_reason"),
        )
