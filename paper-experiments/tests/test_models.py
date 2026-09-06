from types import SimpleNamespace

import pytest

from paper_experiments.models import ax_llm_config, langchain_usage, mini_model_kwargs


def test_gemini_billed_thoughts_and_cache_are_disjoint():
    response = SimpleNamespace(
        usage_metadata={
            "input_tokens": 100,
            "output_tokens": 60,
            "input_token_details": {"cache_read": 20},
            "output_token_details": {"reasoning": 50},
        }
    )
    assert langchain_usage(response).billable() == {
        "input": 80,
        "output": 10,
        "reasoning": 50,
        "cache_read": 20,
        "cache_write": 0,
    }


def test_missing_usage_does_not_silently_become_free():
    with pytest.raises(ValueError, match="usage"):
        langchain_usage(SimpleNamespace(usage_metadata=None))


def test_matching_google_thinking_and_disabled_hidden_retries():
    settings = {
        "provider": "google",
        "ax_model": "google_genai:gemini-3.8-flash",
        "thinking": {"level": "high"},
        "temperature": 1,
        "max_output_tokens": 32768,
        "request_timeout_seconds": 600,
    }
    ax = ax_llm_config(settings)
    mini = mini_model_kwargs(settings)
    assert ax.provider_config["thinking_level"] == mini["reasoning_effort"] == "high"
    assert ax.provider_config["retries"] == 1
    assert ax.retry_config == {}
    assert mini["num_retries"] == 0
