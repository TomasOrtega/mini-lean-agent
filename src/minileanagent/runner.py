"""Construct and evaluate an unmodified mini-SWE-agent run on Lean."""

from __future__ import annotations

import os
import shlex
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from minileanagent import __version__
from minileanagent.config import load_config, recursive_merge
from minileanagent.verify import (
    VerificationResult,
    project_lean_command,
    resolve_target,
    resolve_verification_project,
    validate_declaration_name,
    verify_target,
)


@dataclass(frozen=True)
class UpstreamAPI:
    """The deliberately small public surface imported from mini-SWE-agent."""

    default_agent: type[Any]
    local_environment: type[Any]
    get_model: Callable[..., Any]
    version: str


@dataclass(frozen=True)
class RunOutcome:
    """Result of one upstream agent run plus Lean-side evaluation."""

    status: str
    verified: bool
    trajectory_path: Path
    upstream_exit_status: str
    upstream_submission: str
    upstream_result: dict[str, Any]
    verification: VerificationResult
    model_cost: float
    model_calls: int

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "verified": self.verified,
            "trajectory_path": str(self.trajectory_path),
            "upstream_exit_status": self.upstream_exit_status,
            "upstream_submission": self.upstream_submission,
            "upstream_result": self.upstream_result,
            "verification": self.verification.to_dict(),
            "model_cost": self.model_cost,
            "model_calls": self.model_calls,
        }


def _load_upstream() -> UpstreamAPI:
    """Load mini-SWE-agent lazily so verifier-only commands stay lightweight."""
    os.environ.setdefault("MSWEA_SILENT_STARTUP", "1")
    from minisweagent import __version__ as upstream_version
    from minisweagent.agents.default import DefaultAgent
    from minisweagent.environments.local import LocalEnvironment
    from minisweagent.models import get_model

    return UpstreamAPI(
        default_agent=DefaultAgent,
        local_environment=LocalEnvironment,
        get_model=get_model,
        version=upstream_version,
    )


def installed_upstream_version() -> str:
    """Return the installed mini-SWE-agent distribution version."""
    try:
        return version("mini-swe-agent")
    except PackageNotFoundError:
        return "not installed"


def default_trajectory_path(project: Path) -> Path:
    """Choose a collision-resistant trajectory path inside the Lean project."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    return project / ".mini-lean-agent" / "trajectories" / f"{stamp}.traj.json"


def _validate_limits(
    *,
    step_limit: int,
    cost_limit: float,
    wall_time_limit_seconds: int,
    command_timeout_seconds: int,
) -> None:
    if step_limit < 0:
        raise ValueError("step_limit must be non-negative")
    if cost_limit < 0:
        raise ValueError("cost_limit must be non-negative")
    if wall_time_limit_seconds < 0:
        raise ValueError("wall_time_limit_seconds must be non-negative")
    if command_timeout_seconds <= 0:
        raise ValueError("command_timeout_seconds must be positive")


def build_agent(
    *,
    project: Path | str,
    target: Path | str,
    model_name: str | None,
    trajectory_path: Path,
    declaration: str,
    step_limit: int = 30,
    cost_limit: float = 3.0,
    wall_time_limit_seconds: int = 900,
    command_timeout_seconds: int = 120,
    lean_command: str | None = None,
    model_class: str | None = None,
    model_config: Mapping[str, Any] | None = None,
    lean_config_path: Path | str | None = None,
) -> Any:
    """Construct upstream ``DefaultAgent``, model, and ``LocalEnvironment``."""
    declaration = validate_declaration_name(declaration)
    _validate_limits(
        step_limit=step_limit,
        cost_limit=cost_limit,
        wall_time_limit_seconds=wall_time_limit_seconds,
        command_timeout_seconds=command_timeout_seconds,
    )
    upstream = _load_upstream()
    project_path, target_relative, _ = resolve_target(project, target)
    config = load_config(lean_config_path)

    packaged_model = config.get("model", {})
    if not isinstance(packaged_model, Mapping):
        raise ValueError("The Lean config's model section must be a mapping")
    resolved_model = recursive_merge(packaged_model, model_config or {})
    if model_class:
        resolved_model["model_class"] = model_class
    model = upstream.get_model(model_name, config=resolved_model)

    packaged_environment = config.get("environment", {})
    if not isinstance(packaged_environment, Mapping):
        raise ValueError("The Lean config's environment section must be a mapping")
    environment_config = recursive_merge(packaged_environment)
    environment_env = environment_config.pop("env", {})
    if not isinstance(environment_env, Mapping):
        raise ValueError("The Lean config's environment.env section must be a mapping")
    child_env = {str(key): str(value) for key, value in environment_env.items()}
    environment_config.pop("cwd", None)
    environment_config.pop("timeout", None)
    environment = upstream.local_environment(
        cwd=str(project_path),
        env=child_env,
        timeout=command_timeout_seconds,
        **environment_config,
    )

    packaged_agent = config.get("agent", {})
    if not isinstance(packaged_agent, Mapping):
        raise ValueError("The Lean config's agent section must be a mapping")
    agent_config = recursive_merge(packaged_agent)
    agent_config.update(
        {
            "step_limit": step_limit,
            "cost_limit": cost_limit,
            "wall_time_limit_seconds": wall_time_limit_seconds,
            "output_path": trajectory_path,
        }
    )
    agent = upstream.default_agent(model, environment, **agent_config)
    agent.extra_template_vars.update(
        {
            "lean_project": str(project_path),
            "lean_target": str(target_relative),
            "lean_declaration": declaration,
            "lean_check_command": shlex.join(
                project_lean_command(project_path, target_relative, lean_command)
            ),
        }
    )
    return agent


def _status_from_results(verified: bool, upstream_exit_status: str) -> str:
    if verified:
        return "success"
    if upstream_exit_status == "Submitted":
        return "verification_failed"
    return upstream_exit_status or "verification_failed"


def run_task(
    task: str,
    *,
    project: Path | str,
    target: Path | str,
    declaration: str,
    verification_project: Path | str,
    comparator: str = "comparator",
    model_name: str | None = None,
    trajectory_path: Path | None = None,
    step_limit: int = 30,
    cost_limit: float = 3.0,
    wall_time_limit_seconds: int = 900,
    command_timeout_seconds: int = 120,
    verification_timeout_seconds: int = 120,
    lean_command: str | None = None,
    model_class: str | None = None,
    model_config: Mapping[str, Any] | None = None,
    lean_config_path: Path | str | None = None,
) -> RunOutcome:
    """Run upstream mini-SWE-agent unchanged, then evaluate the fixed target."""
    declaration = validate_declaration_name(declaration)
    if verification_timeout_seconds <= 0:
        raise ValueError("verification_timeout_seconds must be positive")
    project_path, target_relative, _ = resolve_target(project, target)
    verifier_path = resolve_verification_project(project_path, verification_project)
    output_path = (
        trajectory_path.expanduser().resolve()
        if trajectory_path is not None
        else default_trajectory_path(project_path)
    )

    agent = build_agent(
        project=project_path,
        target=target_relative,
        model_name=model_name,
        trajectory_path=output_path,
        declaration=declaration,
        step_limit=step_limit,
        cost_limit=cost_limit,
        wall_time_limit_seconds=wall_time_limit_seconds,
        command_timeout_seconds=command_timeout_seconds,
        lean_command=lean_command,
        model_class=model_class,
        model_config=model_config,
        lean_config_path=lean_config_path,
    )

    raw_upstream_result = agent.run(task)
    if not isinstance(raw_upstream_result, dict):
        raise TypeError("mini-SWE-agent returned a non-mapping run result")
    upstream_result: dict[str, Any] = dict(raw_upstream_result)
    upstream_exit_status = str(upstream_result.get("exit_status", ""))
    upstream_submission = str(upstream_result.get("submission", ""))

    verification = verify_target(
        project_path,
        target_relative,
        declaration=declaration,
        verification_project=verifier_path,
        comparator=comparator,
        timeout_seconds=verification_timeout_seconds,
    )
    verified = verification.ok
    status = _status_from_results(verified, upstream_exit_status)

    upstream = _load_upstream()
    metadata = {
        "version": __version__,
        "upstream_distribution": "mini-swe-agent",
        "upstream_version": installed_upstream_version(),
        "upstream_runtime_version": upstream.version,
        "project": str(project_path),
        "target": str(target_relative),
        "declaration": declaration,
        "verification_project": str(verifier_path),
        "status": status,
        "verified": verified,
        "upstream_exit_status": upstream_exit_status,
        "verification": verification.to_dict(),
    }
    # Preserve the upstream trajectory schema and add one namespaced metadata object.
    agent.save(output_path, {"info": {"mini_lean_agent": metadata}})

    return RunOutcome(
        status=status,
        verified=verified,
        trajectory_path=output_path,
        upstream_exit_status=upstream_exit_status,
        upstream_submission=upstream_submission,
        upstream_result=upstream_result,
        verification=verification,
        model_cost=float(getattr(agent, "cost", 0.0)),
        model_calls=int(getattr(agent, "n_calls", 0)),
    )
