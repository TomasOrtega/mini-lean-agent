"""Pinned model settings and usage normalization for the AxProver adapter."""

from __future__ import annotations

import os
import re
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from typing import Any

from .ledger import Usage, dollars


def redact_provider_text(message: str) -> str:
    for name in (
        "OPENROUTER_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GOOGLE_API_KEY",
        "GEMINI_API_KEY",
    ):
        if value := os.environ.get(name):
            message = message.replace(value, "[REDACTED]")
    return re.sub(r"(?i)Bearer\s+[^\s,;\"']+", "Bearer [REDACTED]", message)


def redact_provider_error(error: BaseException) -> str:
    return redact_provider_text(f"{type(error).__name__}: {error}")


def provider_billing(response: Any, *, openrouter: bool) -> dict[str, Any] | None:
    if not openrouter:
        return None
    metadata = getattr(response, "response_metadata", {}) or {}
    raw = response.model_dump(mode="json") if hasattr(response, "model_dump") else {}
    usage = metadata.get("usage") or raw.get("usage") or metadata
    is_byok = usage.get("is_byok")
    if not isinstance(is_byok, bool):
        raise ValueError("OpenRouter response omitted its boolean BYOK billing flag")

    def amount(value: Any, name: str) -> Decimal:
        if value is None or isinstance(value, bool):
            raise ValueError(f"OpenRouter response omitted its {name}")
        try:
            return dollars(str(value))
        except (ValueError, InvalidOperation) as error:
            raise ValueError(f"OpenRouter response has an invalid {name}") from error

    router_charge = amount(usage.get("cost"), "actual charge")
    details = usage.get("cost_details") or {}
    upstream_raw = details.get("upstream_inference_cost")
    upstream = (
        amount(upstream_raw, "upstream inference cost")
        if is_byok or upstream_raw is not None
        else None
    )
    total = router_charge + (upstream if is_byok else Decimal(0))
    return {
        "is_byok": is_byok,
        "openrouter_charge_usd": str(router_charge),
        "upstream_inference_cost_usd": str(upstream) if upstream is not None else None,
        "total_charge_usd": str(total),
        "charge_rule": "openrouter_plus_upstream" if is_byok else "openrouter",
    }


def provider_charge(response: Any, *, openrouter: bool) -> Decimal | None:
    billing = provider_billing(response, openrouter=openrouter)
    return dollars(billing["total_charge_usd"]) if billing is not None else None


def openrouter_model(settings: dict[str, Any], *, client: Any = None) -> Any:
    from langchain_openrouter import ChatOpenRouter

    class BillingChatOpenRouter(ChatOpenRouter):
        def _create_chat_result(self, response: Any) -> Any:
            raw = response if isinstance(response, dict) else response.model_dump(by_alias=True)
            result = super()._create_chat_result(response)
            for generation in result.generations:
                # The SDK otherwise drops is_byok, making total billing ambiguous.
                generation.message.response_metadata["usage"] = deepcopy(raw.get("usage") or {})
            return result

    return BillingChatOpenRouter(
        model=settings["requested_model"], client=client, **ax_llm_config(settings).provider_config
    )


def openrouter_reasoning(settings: dict[str, Any]) -> dict[str, Any]:
    thinking = settings["thinking"]
    if thinking in ({"level": "high"}, {"level": "max"}):
        return {"effort": thinking["level"]}
    if thinking == {"type": "enabled", "budget_tokens": 16384}:
        return {"max_tokens": 16384, "exclude": False}
    raise ValueError("Unregistered OpenRouter thinking settings")


def langchain_usage(response: Any) -> Usage:
    """LangChain 4.4 Gemini counters include thinking in output and cache in input."""
    usage = response.usage_metadata
    if not usage or not usage.get("input_tokens") or "output_tokens" not in usage:
        raise ValueError("Provider response has no complete usage metadata")
    inputs = usage.get("input_token_details", {})
    outputs = usage.get("output_token_details", {})
    return Usage(
        input_tokens=usage["input_tokens"],
        output_tokens=usage["output_tokens"],
        reasoning_tokens=outputs.get("reasoning", 0),
        cache_read_tokens=inputs.get("cache_read", 0),
        cache_write_tokens=inputs.get("cache_creation", 0),
        reasoning_in_output=True,
        cache_in_input=True,
        reasoning_reported="reasoning" in outputs,
    )


def ax_llm_config(settings: dict[str, Any]) -> Any:
    from ax_prover.config import LLMConfig

    if settings["provider"] == "openrouter":
        return LLMConfig(
            model=settings["ax_model"],
            provider_config={
                "temperature": settings["temperature"],
                "max_tokens": settings["max_output_tokens"],
                "reasoning": openrouter_reasoning(settings),
                "openrouter_provider": settings["openrouter_provider"],
                "max_retries": 0,
                # max_retries=0 alone leaves the official SDK's default retries enabled.
                "model_kwargs": {"retries": None},
                "timeout": settings["request_timeout_seconds"] * 1000,
                "profile": {"max_input_tokens": settings["context_length"]},
                "cache": False,
            },
            retry_config={},
        )
    if settings["provider"] != "google" or settings["thinking"] != {"level": "high"}:
        raise ValueError("The matched AxProver experiment requires Gemini with high thinking")
    return LLMConfig(
        model=settings["ax_model"],
        provider_config={
            "temperature": settings["temperature"],
            "max_tokens": settings["max_output_tokens"],
            "thinking_level": "high",
            "include_thoughts": True,
            "retries": 1,
            "request_timeout": settings["request_timeout_seconds"],
            "cache": False,
        },
        retry_config={},
    )


def mini_model_kwargs(settings: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "max_tokens": settings["max_output_tokens"],
        "num_retries": 0,
        "timeout": settings["request_timeout_seconds"],
    }
    if settings.get("temperature") is not None:
        result["temperature"] = settings["temperature"]
    if settings["provider"] == "openrouter":
        result["extra_body"] = {
            "reasoning": openrouter_reasoning(settings),
            "provider": settings["openrouter_provider"],
            "usage": {"include": True},
        }
        if "service_tier" in settings:
            result["extra_body"]["service_tier"] = settings["service_tier"]
    elif settings["provider"] == "google":
        result["reasoning_effort"] = settings["thinking"]["level"]
    else:
        result["thinking"] = settings["thinking"]
    return result
