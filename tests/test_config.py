from __future__ import annotations

from pathlib import Path

import pytest

from minileanagent.config import config_path, load_config, recursive_merge


@pytest.fixture(autouse=True)
def fake_upstream_mini_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep unit tests offline while the integration test exercises the real package."""
    monkeypatch.setattr(
        "minileanagent.config._load_upstream_config",
        lambda: {
            "agent": {
                "system_template": "upstream system",
                "instance_template": "upstream {{task}}",
                "step_limit": 0,
                "cost_limit": 3.0,
            },
            "environment": {"env": {"PAGER": "cat"}},
            "model": {
                "observation_template": "{{ output.output }}",
                "format_error_template": "{{ error }}",
                "model_kwargs": {"drop_params": True},
            },
        },
    )


def test_packaged_config_is_upstream_style() -> None:
    config = load_config()
    system_template = config["agent"]["system_template"]
    instance_template = config["agent"]["instance_template"]
    assert set(config) == {"agent", "environment", "model"}
    assert "helpful Lean 4 theorem-proving assistant" in system_template
    assert "Recommended workflow" in instance_template
    assert "after each proof attempt" in instance_template
    assert "Read every error or warning" in instance_template
    assert "Do not change or weaken" in instance_template
    assert "new axioms" in instance_template
    assert "Use non-interactive commands" in instance_template
    assert "mini-lean-submit" in instance_template
    assert "lean-specific action interface" in instance_template.lower()
    assert "observation_template" in config["model"]
    assert config_path().name == "lean.yaml"
    assert config_path().is_file()


def test_load_config_returns_fresh_data() -> None:
    first = load_config()
    first["agent"]["cost_limit"] = 999
    assert load_config()["agent"]["cost_limit"] == 3.0


def test_user_config_is_an_overlay(tmp_path: Path) -> None:
    overlay = tmp_path / "overlay.yaml"
    overlay.write_text("agent:\n  cost_limit: 1.5\nmodel:\n  model_kwargs:\n    temperature: 0\n")
    config = load_config(overlay)
    assert config["agent"]["cost_limit"] == 1.5
    assert "instance_template" in config["agent"]
    assert config["model"]["model_kwargs"]["temperature"] == 0
    assert config["model"]["model_kwargs"]["drop_params"] is True


def test_config_must_be_mapping(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text("- not\n- a\n- mapping\n", encoding="utf-8")
    with pytest.raises(ValueError, match="mapping"):
        load_config(path)


def test_recursive_merge_does_not_mutate_inputs() -> None:
    first = {"a": {"b": 1}, "x": 1}
    second = {"a": {"c": 2}}
    assert recursive_merge(first, second) == {"a": {"b": 1, "c": 2}, "x": 1}
    assert first == {"a": {"b": 1}, "x": 1}
