"""Accounting and snapshot hooks around the installed, unchanged upstream loop."""

from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
import time
from collections.abc import Callable
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from typing import Any, ClassVar

import litellm
from minileanagent.config import load_config
from minisweagent.agents.default import DefaultAgent
from minisweagent.environments.docker import DockerEnvironment
from minisweagent.models.litellm_model import LitellmModel

from .cache import cache_mount
from .controller import RunController, RunStopped, atomic_json
from .ledger import AccountingIncomplete, Usage
from .models import provider_billing, redact_provider_error, redact_provider_text
from .protocol import GEMINI, canonical_json, content_hash
from .resources import GIB, ContainerCleanupError, ensure_container_removed, resolve_resources


def normalize_litellm_usage(response: Any, model: str, *, openrouter: bool = False) -> Usage | None:
    """LiteLLM normalizes Gemini/Anthropic output and prompt totals to inclusive counters."""
    raw = response.model_dump(mode="json").get("usage")
    if not raw or not raw.get("prompt_tokens") or raw.get("completion_tokens") is None:
        return None
    prompt = raw.get("prompt_tokens_details") or {}
    completion = raw.get("completion_tokens_details") or {}
    # Anthropic's reasoning split can be an adapter estimate; total output remains billable.
    reasoning_reported = (model == GEMINI or openrouter) and completion.get(
        "reasoning_tokens"
    ) is not None
    return Usage(
        input_tokens=raw["prompt_tokens"],
        output_tokens=raw["completion_tokens"],
        reasoning_tokens=completion["reasoning_tokens"] if reasoning_reported else 0,
        cache_read_tokens=prompt.get("cached_tokens") or raw.get("cache_read_input_tokens") or 0,
        cache_write_tokens=prompt.get("cache_creation_tokens")
        or raw.get("cache_creation_input_tokens")
        or 0,
        reasoning_in_output=True,
        cache_in_input=True,
        reasoning_reported=reasoning_reported,
    )


class MeteredLitellmModel(LitellmModel):
    abort_exceptions: ClassVar[list[type[Exception]]] = [
        *LitellmModel.abort_exceptions,
        RunStopped,
        AccountingIncomplete,
    ]

    def __init__(self, controller: RunController, **kwargs: Any):
        model_kwargs = dict(kwargs.get("model_kwargs", {}))
        if model_kwargs.get("num_retries", 0) != 0:
            raise ValueError("SDK retries must be disabled so every attempt reaches the ledger")
        model_kwargs["num_retries"] = 0
        kwargs["model_kwargs"] = model_kwargs
        kwargs["set_cache_control"] = None
        super().__init__(**kwargs)
        self.controller = controller
        self.last_charge = 0.0
        self.boundary_state: Callable[[], tuple[Path, dict[str, Any]]] | None = None
        self.query_pending = False
        self.attempts_in_query = 0

    def query(self, messages: list[dict[str, str]], **kwargs: Any) -> dict[str, Any]:
        if not self.query_pending:
            self.attempts_in_query = 0
        self.query_pending = True
        previous = os.environ.get("MSWEA_MODEL_RETRY_STOP_AFTER_ATTEMPT")
        remaining = int(previous or "10") - self.attempts_in_query
        if remaining <= 0:
            self.query_pending = False
            raise RuntimeError("The frozen logical request exhausted its retry allowance")
        os.environ["MSWEA_MODEL_RETRY_STOP_AFTER_ATTEMPT"] = str(remaining)
        try:
            result = super().query(messages, **kwargs)
        except RunStopped:
            raise
        except Exception:
            self.query_pending = False
            raise
        else:
            self.query_pending = False
            return result
        finally:
            if previous is None:
                os.environ.pop("MSWEA_MODEL_RETRY_STOP_AFTER_ATTEMPT", None)
            else:
                os.environ["MSWEA_MODEL_RETRY_STOP_AFTER_ATTEMPT"] = previous

    def _query(self, messages: list[dict[str, str]], **kwargs: Any) -> Any:
        candidate, driver_state = self.boundary_state() if self.boundary_state else (None, None)
        self.controller.before_request(candidate=candidate, driver_state=driver_state)
        self.attempts_in_query += 1
        label = self.controller.state["run"]["model"]
        ledger = self.controller.ledger
        attempt_id = ledger.begin_attempt(
            "proposer", label, {"requested_model": self.config.model_name}
        )
        self.controller._persist()
        try:
            response = super()._query(messages, **kwargs)
        except Exception as error:
            safe_error = redact_provider_error(error)
            error.args = (safe_error,)
            if hasattr(error, "message"):
                error.message = safe_error
            # Rejected requests were not served. Transport/server timeouts remain unknown.
            rejected = isinstance(
                error,
                (
                    litellm.RateLimitError,
                    litellm.AuthenticationError,
                    litellm.PermissionDeniedError,
                    litellm.BadRequestError,
                ),
            )
            ledger.finish_attempt(
                attempt_id,
                None,
                actual_charge="0" if rejected else None,
                error=safe_error,
                metadata={"exception_type": type(error).__name__},
            )
            if not rejected:
                raise AccountingIncomplete(
                    "Provider error has unknown billing; reconcile before retry"
                ) from error
            raise
        openrouter = self.config.model_name.startswith("openrouter/")
        if openrouter:
            for choice in response.choices:
                fields = getattr(choice.message, "provider_specific_fields", None) or {}
                if "reasoning_details" in fields:
                    # LiteLLM nests OpenRouter's opaque reasoning state on receipt.
                    choice.message.reasoning_details = deepcopy(fields["reasoning_details"])
        raw = response.model_dump(mode="json")
        metadata = {
            "requested_model": self.config.model_name,
            "returned_model": raw.get("model"),
            "response_id": raw.get("id"),
            "provider": raw.get("provider"),
            "usage": raw.get("usage"),
        }
        try:
            billing = provider_billing(response, openrouter=openrouter)
            charge = Decimal(billing["total_charge_usd"]) if billing is not None else None
            metadata["billing"] = billing
            usage = normalize_litellm_usage(response, label, openrouter=openrouter)
            ledger.finish_attempt(attempt_id, usage, actual_charge=charge, metadata=metadata)
        except Exception as error:
            ledger.finish_attempt(
                attempt_id,
                None,
                error=f"Usage normalization failed: {redact_provider_error(error)}",
                metadata=metadata,
            )
            raise AccountingIncomplete("Response usage could not be normalized") from error
        if usage is None:
            raise AccountingIncomplete("Response omitted usage; reconcile before retry")
        self.last_charge = float(
            charge if charge is not None else ledger.prices.charge(label, usage)
        )
        return response

    def _calculate_cost(self, response: Any) -> dict[str, float]:
        return {"cost": self.last_charge}


class DurableDockerEnvironment(DockerEnvironment):
    """Keep the actual container alive across the documented administrative pause."""

    def __init__(
        self,
        *,
        resume_container: str | None = None,
        container_record: Path | None = None,
        **kwargs: Any,
    ):
        self._resume_container = resume_container
        self._container_record = container_record
        super().__init__(**kwargs)

    def _start_container(self) -> None:
        if self._resume_container is None:
            record = getattr(self, "_container_record", None)
            if record:
                name = "mini-lean-paper-" + content_hash(str(record.resolve()))[:32]
                atomic_json(record, {"container_name": name})
                self.config.run_args = [*self.config.run_args, "--name", name]
                try:
                    super()._start_container()
                except BaseException as error:
                    ensure_container_removed(name, by_name=True)
                    raise ContainerCleanupError(
                        f"Docker startup for {name} was interrupted; stopping queue admission"
                    ) from error
            else:
                super()._start_container()
            self._record_container()
            return
        if not self._resume_container.isalnum():
            raise ValueError("Invalid frozen Docker container ID")
        result = subprocess.run(
            [self.config.executable, "inspect", self._resume_container],
            check=True,
            capture_output=True,
            text=True,
        )
        details = json.loads(result.stdout)[0]
        if not details["State"]["Running"]:
            raise ValueError("Frozen container no longer runs; refusing a fresh replacement")
        if (
            details["HostConfig"]["NetworkMode"] != "none"
            or details["Config"]["Image"] != self.config.image
        ):
            raise ValueError("Frozen container's image or network boundary changed")
        arguments = self.config.run_args
        expected = {
            "NanoCpus": int(arguments[arguments.index("--cpus") + 1]) * 10**9,
            "Memory": int(arguments[arguments.index("--memory") + 1].removesuffix("g")) * GIB,
            "MemorySwap": int(arguments[arguments.index("--memory-swap") + 1].removesuffix("g"))
            * GIB,
        }
        if any(details["HostConfig"].get(key) != value for key, value in expected.items()):
            raise ValueError("Frozen container's resource limits changed")
        for argument in arguments:
            if argument.startswith("type=volume,"):
                mount = dict(part.split("=", 1) for part in argument.split(",") if "=" in part)
                if not any(
                    actual.get("Type") == "volume"
                    and actual.get("Name") == mount["src"]
                    and actual.get("Destination") == mount["dst"]
                    and actual.get("RW") is False
                    for actual in details.get("Mounts", [])
                ):
                    raise ValueError("Frozen container's dependency cache mount changed")
        self.container_id = self._resume_container
        self._record_container()

    def _record_container(self) -> None:
        if record := getattr(self, "_container_record", None):
            saved = json.loads(record.read_text()) if record.exists() else {}
            atomic_json(record, {**saved, "container_id": self.container_id})

    def cleanup(self) -> None:
        # Explicit archive/cleanup owns this durable run resource, never object destruction.
        return None


class SnapshotAgent(DefaultAgent):
    def __init__(
        self,
        *args: Any,
        controller: RunController,
        candidate: Path,
        driver_config: dict[str, Any],
        resume_state: dict[str, Any] | None = None,
        **kwargs: Any,
    ):
        super().__init__(*args, **kwargs)
        self.controller = controller
        self.candidate = candidate
        self.driver_config = driver_config
        self._resume_state = resume_state
        if isinstance(self.model, MeteredLitellmModel):
            self.model.boundary_state = self.request_boundary_state
        if resume_state is not None:
            if resume_state["driver_config"] != driver_config:
                raise ValueError("Cannot resume with changed model, prompt, tools, or environment")
            self.n_calls = resume_state["agent_calls"]
            if isinstance(self.model, MeteredLitellmModel):
                frozen_model = resume_state.get("model_retry_state", {})
                self.model.query_pending = frozen_model.get("query_pending", False)
                self.model.attempts_in_query = frozen_model.get("attempts_in_query", 0)
                if self.model.query_pending:
                    self.n_calls -= 1
            self.cost = resume_state["agent_cost"]
            self.n_consecutive_format_errors = resume_state["consecutive_format_errors"]
            self.extra_template_vars = resume_state["extra_template_vars"]
            self._start_time = time.time() - controller.active_seconds

    def add_messages(self, *messages: dict[str, Any]) -> list[dict[str, Any]]:
        for message in messages:
            if message.get("role") == "exit":
                if isinstance(message.get("content"), str):
                    message["content"] = redact_provider_text(message["content"])
                for key in ("exception_str", "traceback"):
                    if isinstance(message.get("extra", {}).get(key), str):
                        message["extra"][key] = redact_provider_text(message["extra"][key])
        if self._resume_state is not None and not self.messages:
            frozen = self._resume_state["messages"]
            if list(messages) != frozen[:2]:
                raise ValueError("Continuation would change the initial system or task prompt")
            self.messages = json.loads(canonical_json(frozen))
            self._resume_state = None
            return self.messages
        return super().add_messages(*messages)

    def request_boundary_state(self) -> tuple[Path, dict[str, Any]]:
        workspace = Path(self.driver_config["workspace"])
        if not self.candidate.resolve().is_relative_to(workspace.resolve()):
            raise ValueError("Target escaped the isolated workspace")
        return self.candidate, self.export_state()

    def export_state(self) -> dict[str, Any]:
        return {
            "adapter": "mini-upstream-hooks-v1",
            "resumable": not self.messages or self.messages[-1].get("role") != "exit",
            "model_retry_state": {
                "query_pending": self.model.query_pending,
                "attempts_in_query": self.model.attempts_in_query,
            }
            if isinstance(self.model, MeteredLitellmModel)
            else {},
            "driver_config": self.driver_config,
            "messages": self.messages,
            "agent_calls": self.n_calls,
            "agent_cost": self.cost,
            "consecutive_format_errors": self.n_consecutive_format_errors,
            "extra_template_vars": self.extra_template_vars,
            "container_id": self.env.container_id,
        }

    def handle_uncaught_exception(self, error: Exception) -> list[dict[str, Any]]:
        if isinstance(error, ContainerCleanupError):
            raise error
        if isinstance(error, RunStopped):
            return []
        return super().handle_uncaught_exception(error)

    def save(self, path: Path | None, *extra_dicts: Any) -> dict[str, Any]:
        data = super().save(path, *extra_dicts)
        if self.controller.state["paused"]:
            return data
        workspace = Path(self.driver_config["workspace"])
        if not self.candidate.resolve().is_relative_to(workspace.resolve()):
            raise ValueError("Target escaped the isolated workspace")
        exit_status = (
            self.messages[-1].get("extra", {}).get("exit_status", "") if self.messages else ""
        )
        stop_reason = None
        if self.messages and self.messages[-1].get("role") == "exit":
            stop_reason = (
                "context_capacity"
                if "ContextWindow" in exit_status
                else "agent_exit"
                if exit_status in {"Submitted", "RepeatedFormatError"}
                else "infrastructure_failure"
            )
        self.controller.after_boundary(
            self.candidate,
            self.export_state(),
            stop_reason=stop_reason,
            boundary="exit" if stop_reason else "upstream_step",
        )
        if self.controller.state["paused"]:
            raise RunStopped(self.controller.state["endpoint"]["stop_reason"])
        return data


def run_mini(
    controller: RunController,
    *,
    workspace: Path,
    target: Path,
    declaration: str,
    task: str,
    model_config: dict[str, Any],
    image: str,
    resources: dict[str, Any] | None = None,
    dependency_cache: dict[str, Any] | None = None,
    resume_state: dict[str, Any] | None = None,
    command_timeout_seconds: int = 120,
    retry_attempts: int = 3,
) -> dict[str, Any]:
    """Run upstream DefaultAgent.run, restoring its exact messages on continuation."""
    if controller.state["paused"]:
        return controller.state["endpoint"]
    if "@sha256:" not in image and not image.startswith("sha256:"):
        raise ValueError("Agent images must be pinned by digest")
    if dependency_cache is not None and dependency_cache["image"] != image:
        raise ValueError("Dependency cache and agent image differ")
    workspace = workspace.resolve()
    target = target if target.is_absolute() else workspace / target
    target_relative = target.resolve().relative_to(workspace)
    if retry_attempts < 1:
        raise ValueError("At least one provider attempt must be allowed")
    profile = resolve_resources(resources)
    config = load_config()
    agent_config = config["agent"] | {
        "step_limit": 0,
        "cost_limit": 0,
        "wall_time_limit_seconds": 0,
        "output_path": str(controller.output_dir / "trajectory.json"),
    }
    environment_config = {
        "image": image,
        "cwd": "/workspace",
        "forward_env": [],
        "env": config.get("environment", {}).get("env", {}),
        "timeout": command_timeout_seconds,
        "container_timeout": "168h",
        "run_args": [
            "--network=none",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--pids-limit=512",
            *profile.container_args("agent"),
            "--mount",
            f"type=bind,src={workspace},dst=/workspace",
            *(cache_mount(dependency_cache) if dependency_cache is not None else []),
        ],
    }
    driver_config = {
        "workspace": str(workspace),
        "target": str(target_relative),
        "declaration": declaration,
        "task": task,
        "model": model_config,
        "agent": agent_config,
        "environment": environment_config,
        "retry_attempts": retry_attempts,
        "resources": profile.to_dict(),
        "dependency_cache": dependency_cache,
    }
    if resume_state is not None and resume_state["driver_config"] != driver_config:
        raise ValueError("Frozen run configuration does not match the requested continuation")
    env = DurableDockerEnvironment(
        resume_container=resume_state["container_id"] if resume_state else None,
        container_record=controller.output_dir / "agent-container.json",
        **environment_config,
    )
    model = MeteredLitellmModel(controller, **model_config)
    agent = SnapshotAgent(
        model,
        env,
        controller=controller,
        candidate=target,
        driver_config=driver_config,
        resume_state=resume_state,
        **agent_config,
    )
    if resume_state is None:
        agent.extra_template_vars.update(
            {
                "lean_project": "/workspace",
                "lean_target": str(target_relative),
                "lean_declaration": declaration,
                "lean_check_command": shlex.join(["lake", "env", "lean", str(target_relative)]),
            }
        )
    previous_retry = os.environ.get("MSWEA_MODEL_RETRY_STOP_AFTER_ATTEMPT")
    os.environ["MSWEA_MODEL_RETRY_STOP_AFTER_ATTEMPT"] = str(retry_attempts)
    try:
        agent.run(task)
    except RunStopped:
        pass
    finally:
        if previous_retry is None:
            os.environ.pop("MSWEA_MODEL_RETRY_STOP_AFTER_ATTEMPT", None)
        else:
            os.environ["MSWEA_MODEL_RETRY_STOP_AFTER_ATTEMPT"] = previous_retry
    logging.getLogger(__name__).info(
        "Preserved Docker container %s for run %s (config %s)",
        env.container_id,
        controller.state["run"]["run_id"],
        content_hash(driver_config),
    )
    return controller.state["endpoint"] or controller.state
