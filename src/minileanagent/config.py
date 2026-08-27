"""Configuration helpers for the mini-SWE-agent specialization."""

from __future__ import annotations

import os
from importlib.resources import files
from pathlib import Path
from typing import Any, cast

import yaml

_CONFIG_RESOURCE = files("minileanagent").joinpath("config", "lean.yaml")


def recursive_merge(*dictionaries: dict[str, Any] | None) -> dict[str, Any]:
    """Use mini-SWE-agent's merge semantics without importing it at module import time."""
    os.environ.setdefault("MSWEA_SILENT_STARTUP", "1")
    from minisweagent.utils.serialize import recursive_merge as upstream_recursive_merge

    return cast(dict[str, Any], upstream_recursive_merge(*dictionaries))


def load_mapping(path: Path | str) -> dict[str, Any]:
    """Load a YAML mapping from disk."""
    resolved = Path(path).expanduser()
    data = yaml.safe_load(resolved.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Configuration must be a mapping: {resolved}")
    return data


def _load_upstream_config() -> dict[str, Any]:
    """Load mini-SWE-agent's own native-tool-calling configuration."""
    os.environ.setdefault("MSWEA_SILENT_STARTUP", "1")
    from minisweagent.config import get_config_from_spec

    data = get_config_from_spec("mini.yaml")
    if not isinstance(data, dict):
        raise ValueError("mini-SWE-agent's mini.yaml configuration must be a mapping")
    return data


def load_config(overlay: Path | str | None = None) -> dict[str, Any]:
    """Overlay the packaged Lean settings on upstream ``mini.yaml``."""
    lean_overlay = yaml.safe_load(_CONFIG_RESOURCE.read_text(encoding="utf-8"))
    if not isinstance(lean_overlay, dict):
        raise ValueError("The packaged Lean configuration must be a mapping")
    merged = recursive_merge(_load_upstream_config(), lean_overlay)
    if overlay is None:
        return merged
    return recursive_merge(merged, load_mapping(overlay))


def config_path() -> Path:
    """Return the installed path to the packaged Lean overlay."""
    return Path(str(_CONFIG_RESOURCE))


__all__ = ["config_path", "load_config", "load_mapping", "recursive_merge"]
