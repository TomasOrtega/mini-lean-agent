"""Instrument the pinned AxProverBase graph without replacing its proving nodes."""

from __future__ import annotations

import asyncio
import contextvars
import json
from contextlib import AsyncExitStack
from dataclasses import asdict
from pathlib import Path
from typing import Any
from unittest.mock import patch

from langchain_core.runnables import RunnableConfig

from .ax_sandbox import LeanSandbox
from .controller import RunController, RunStopped, atomic_json
from .models import (
    ax_llm_config,
    langchain_usage,
    openrouter_model,
    provider_billing,
    redact_provider_error,
)
from .protocol import call_limit, canonical_json, content_hash
from .resources import ContainerCleanupError, resolve_resources

_SCOPE: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar("ax_node_scope")


def request_record(messages: Any, tools: Any, schema: Any) -> dict[str, Any]:
    from langchain_core.messages import messages_to_dict

    return {
        "messages": messages_to_dict(messages),
        "tools": [
            {
                "name": tool.name,
                "description": tool.description,
                "schema": tool.args_schema.model_json_schema(),
            }
            for tool in tools or []
        ],
        "output_schema": schema.model_json_schema() if schema else None,
    }


def make_client(
    controller: RunController,
    settings: dict[str, Any],
    transcript_dir: Path,
    *,
    candidate: Path | None = None,
    driver_state: dict[str, Any] | None = None,
) -> Any:
    from ax_prover.utils.llm import LLMClient
    from langchain_core.messages import message_to_dict, messages_from_dict

    class MeteredClient(LLMClient):
        def __init__(self, config: Any):
            super().__init__(config)
            if settings["provider"] == "openrouter":
                self._base_llm = openrouter_model(settings, client=self._base_llm.client)

        def _structured_output_bind_kwargs(self, schema: Any) -> dict[str, Any]:
            if settings["provider"] == "openrouter":
                return {
                    "response_format": {
                        "type": "json_schema",
                        "json_schema": {
                            "name": schema.__name__,
                            "schema": schema.model_json_schema(),
                            "strict": True,
                        },
                    }
                }
            # Pinned upstream passes a dict to a helper expecting a model class.
            return {
                "response_mime_type": "application/json",
                "response_json_schema": schema.model_json_schema(),
            }

        async def ainvoke(
            self,
            messages: Any,
            tools: Any = None,
            output_schema: Any = None,
            retry_config: Any = None,
        ) -> Any:
            scope = _SCOPE.get()
            ordinal = scope["calls"]
            scope["calls"] += 1
            request = request_record(messages, tools, output_schema)
            request_hash = content_hash(request)
            record_path = transcript_dir / f"{scope['step']:06d}-{scope['node']}-{ordinal:04d}.json"
            if record_path.exists():
                saved = json.loads(record_path.read_text())
                if (
                    saved["request_sha256"] != request_hash
                    or content_hash(saved["request"]) != request_hash
                ):
                    raise RuntimeError("Resumed node changed its recorded request")
                if saved.get("response"):
                    if content_hash(saved["response"]) != saved.get("response_sha256"):
                        raise RuntimeError("Saved provider response failed its integrity check")
                    attempts = {
                        a["attempt_id"]: a for a in controller.ledger.snapshot()["attempts"]
                    }
                    billed = attempts.get(saved["attempt_id"])
                    expected_metadata = {
                        "requested_model": settings["requested_model"],
                        "request_sha256": request_hash,
                        "graph_step": scope["step"],
                        "ordinal": ordinal,
                    }
                    if (
                        billed is None
                        or billed["model"] != controller.state["run"]["model"]
                        or billed["role"] != scope["node"]
                        or any(billed["metadata"].get(k) != v for k, v in expected_metadata.items())
                    ):
                        raise RuntimeError(
                            "Saved response does not match its registered ledger attempt"
                        )
                    response = messages_from_dict([saved["response"]])[0]
                    usage = langchain_usage(response)
                    billing = provider_billing(
                        response, openrouter=settings["provider"] == "openrouter"
                    )
                    actual_charge = billing["total_charge_usd"] if billing is not None else None
                    if billed["completed"] and billed.get("charge_usd") is not None:
                        if billed.get("usage") != asdict(usage):
                            raise RuntimeError(
                                "Saved response usage differs from its settled ledger attempt"
                            )
                        if actual_charge is not None and str(actual_charge) != billed["charge_usd"]:
                            raise RuntimeError(
                                "Saved provider charge differs from its settled attempt"
                            )
                        if (
                            billing is not None
                            and billed["response_metadata"].get("billing") != billing
                        ):
                            raise RuntimeError(
                                "Saved billing components differ from the settled attempt"
                            )
                    else:
                        controller.ledger.finish_attempt(
                            saved["attempt_id"],
                            usage,
                            actual_charge=actual_charge,
                            metadata={
                                "model_version": response.response_metadata,
                                "billing": billing,
                                "response_file": str(record_path),
                                "recovered_from_response_journal": True,
                            },
                        )
                    return response
            previous_attempts = sum(
                row["role"] == scope["node"]
                and row["metadata"].get("graph_step") == scope["step"]
                and row["metadata"].get("ordinal") == ordinal
                and row["metadata"].get("request_sha256") == request_hash
                for row in controller.ledger.snapshot()["attempts"]
            )
            for attempt in range(previous_attempts, settings["max_attempts"]):
                controller.before_request(candidate=candidate, driver_state=driver_state)
                attempt_id = controller.ledger.begin_attempt(
                    scope["node"],
                    controller.state["run"]["model"],
                    {
                        "requested_model": settings["requested_model"],
                        "request_sha256": request_hash,
                        "graph_step": scope["step"],
                        "ordinal": ordinal,
                    },
                )
                if driver_state is not None:
                    controller.state["driver_state"] = json.loads(canonical_json(driver_state))
                controller._persist()
                atomic_json(
                    record_path,
                    {"request": request, "request_sha256": request_hash, "attempt_id": attempt_id},
                )
                try:
                    response = await super().ainvoke(
                        messages, tools=tools, output_schema=output_schema
                    )
                except Exception as error:
                    status = getattr(error, "status_code", None) or getattr(error, "code", None)
                    # A rejected rate-limit request was not accepted for generation.
                    known_unbilled = status in {400, 401, 403, 404, 429}
                    controller.ledger.finish_attempt(
                        attempt_id,
                        None,
                        actual_charge="0" if known_unbilled else None,
                        error=redact_provider_error(error),
                    )
                    if status == 429 and attempt + 1 < settings["max_attempts"]:
                        await asyncio.sleep(2**attempt)
                        continue
                    if settings["provider"] == "openrouter":
                        raise RuntimeError(redact_provider_error(error)) from None
                    raise
                raw = message_to_dict(response)
                atomic_json(
                    record_path,
                    {
                        "request": request,
                        "request_sha256": request_hash,
                        "attempt_id": attempt_id,
                        "response": raw,
                        "response_sha256": content_hash(raw),
                    },
                )
                try:
                    usage = langchain_usage(response)
                    billing = provider_billing(
                        response, openrouter=settings["provider"] == "openrouter"
                    )
                    actual_charge = billing["total_charge_usd"] if billing is not None else None
                    controller.ledger.finish_attempt(
                        attempt_id,
                        usage,
                        actual_charge=actual_charge,
                        metadata={
                            "model_version": response.response_metadata,
                            "billing": billing,
                            "response_file": str(record_path),
                        },
                    )
                except Exception as error:
                    controller.ledger.finish_attempt(
                        attempt_id,
                        None,
                        error=f"Usage accounting failed: {redact_provider_error(error)}",
                    )
                    if settings["provider"] == "openrouter":
                        raise RuntimeError(redact_provider_error(error)) from None
                    raise
                return response
            raise AssertionError("Request retry loop exhausted")

    return MeteredClient(ax_llm_config(settings))


def _driver_state(
    workspace: Path, configuration: dict[str, Any], *, started: bool
) -> dict[str, Any]:
    return {
        "harness": "axprover",
        "workspace": str(workspace),
        "configuration": configuration,
        "configuration_sha256": content_hash(configuration),
        "graph_started": started,
        "resume": "SQLite graph checkpoint plus immutable per-node provider responses",
    }


async def run_axprover(
    controller: RunController,
    *,
    workspace: Path,
    target: Path,
    declaration: str,
    task: str,
    settings: dict[str, Any],
    image: str,
    retrieval: dict[str, Any],
    resume_state: dict[str, Any] | None = None,
    resources: dict[str, Any] | None = None,
    dependency_cache: dict[str, Any] | None = None,
) -> None:
    from ax_prover.config import MemoryConfig, ProverConfig, RuntimeConfig, SummarizeOutputConfig
    from ax_prover.models.proving import ProverAgentState
    from ax_prover.prover.agent import ProverAgent
    from ax_prover.runtime import Runtime
    from ax_prover.tools.lean_search import _warmup_lean_search
    from ax_prover.tools.registry import create_tool_lifespans
    from ax_prover.utils.build import TemporaryProposal
    from ax_prover.utils.proving import parse_prove_target
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    from .retrieval import (
        NativeSearchIntegrityError,
        checked_session,
        validate_retrieval_live,
    )

    workspace, target = workspace.resolve(), Path(target)
    if resume_state:
        required = ["latest-candidate.lean"]
        if resume_state.get("graph_started"):
            required.append("graph.sqlite")
        for filename in required:
            if not (controller.output_dir / filename).is_file():
                raise ValueError(f"Saved AxProver state is missing: {filename}")
        if resume_state.get("graph_started") and not isinstance(
            resume_state.get("search_records"), dict
        ):
            raise ValueError("Saved AxProver state is missing or malformed: search_records")
    profile = resolve_resources(resources)
    transcript_dir = controller.output_dir / "provider-requests"
    configuration = {
        "settings": settings,
        "task": task,
        "target": str(target),
        "declaration": declaration,
        "image": image,
        "retrieval": retrieval,
        "resources": profile.to_dict(),
        "dependency_cache": dependency_cache,
    }
    if resume_state and (
        resume_state["workspace"] != str(workspace)
        or resume_state["configuration"] != configuration
    ):
        raise ValueError("Cannot resume with different task, model, tools, or workspace")
    llm_config = ax_llm_config(settings)
    config = ProverConfig(
        prover_llm=llm_config,
        proposer_tools={"search_lean": retrieval["tool_config"]},
        max_iterations=call_limit(controller.state["run"]["problem"]) + 1,
        memory_config=MemoryConfig(
            class_name="ExperienceProcessor", init_args={"llm_config": asdict(llm_config)}
        ),
        summarize_output=SummarizeOutputConfig(enabled=True, llm=llm_config),
        user_comments=task,
    )
    sandbox = LeanSandbox(
        workspace,
        image,
        controller.output_dir,
        resume=resume_state.get("sandbox") if resume_state else None,
        resources=profile.to_dict(),
        dependency_cache=dependency_cache,
    )
    runtime_config = RuntimeConfig()
    runtime_config.max_tool_calling_iterations = 5
    runtime_config.lean.max_concurrent_builds = 1
    runtime_config.lean.check_file_timeout = 120
    runtime = Runtime(runtime_config, str(workspace))
    runtime.lean_interact_server = sandbox
    runtime.lean_semaphore = asyncio.Semaphore(1)
    runtime._tool_resources = {}
    latest_candidate = controller.output_dir / "latest-candidate.lean"
    if not latest_candidate.exists():
        latest_candidate.write_bytes((workspace / target).read_bytes())

    class InstrumentedProver(ProverAgent):
        pass

    node_names = (
        "proposer",
        "builder",
        "reviewer",
        "memory_processor",
        "aggregate_metrics",
        "summarize_output",
    )

    def wrap_node(name: str) -> Any:
        original = getattr(ProverAgent, f"_{name}_node")

        async def node(self: Any, state: Any, config: RunnableConfig) -> Any:
            scope = {
                "node": name,
                "step": config["metadata"]["langgraph_step"],
                "calls": 0,
                "search_calls": 0,
            }
            token = _SCOPE.set(scope)
            try:
                if name in {"proposer", "reviewer", "aggregate_metrics"}:
                    result = await original(self, state, config)
                else:
                    result = await original(self, state)
                # Save the proposal as generated, even if the model reviewer later rejects it.
                if name == "proposer":
                    prospective = state.model_copy(deep=True)
                    prospective.messages = [*state.messages, *result.get("messages", [])]
                    if prospective.last_proposal:
                        with TemporaryProposal(
                            str(workspace), prospective.item, prospective.last_proposal
                        ) as applier:
                            if applier.success:
                                latest_candidate.write_bytes(
                                    applier.location.absolute_path(str(workspace)).read_bytes()
                                )
                elif (
                    name == "reviewer"
                    and (workspace / target).read_bytes() != latest_candidate.read_bytes()
                    and state.approved
                ):
                    latest_candidate.write_bytes((workspace / target).read_bytes())
                return result
            finally:
                _SCOPE.reset(token)

        return node

    for name in node_names:
        setattr(InstrumentedProver, f"_{name}_node", wrap_node(name))
    driver = _driver_state(
        workspace, configuration, started=bool(resume_state and resume_state.get("graph_started"))
    )
    driver["sandbox"] = sandbox.state()
    driver["search_records"] = dict(resume_state.get("search_records", {})) if resume_state else {}
    tool_stack = AsyncExitStack()

    def search_request_scope() -> dict[str, Any] | None:
        scope = _SCOPE.get(None)
        if scope is None:
            return None
        ordinal = scope["search_calls"]
        scope["search_calls"] += 1
        return {"node": scope["node"], "step": scope["step"], "search_call": ordinal}

    async def checked_warmup(config: Any, session: Any) -> None:
        protected = checked_session(
            session,
            retrieval,
            controller.output_dir / "leansearch-requests",
            request_scope=search_request_scope,
            archive_records=driver["search_records"],
        )
        runtime._tool_resources["search_lean_search"] = protected
        await _warmup_lean_search(config, protected)

    try:
        await validate_retrieval_live(retrieval)
        with patch("ax_prover.tools.lean_search._warmup_lean_search", checked_warmup):
            for tool_type, lifespan in (await create_tool_lifespans(config.proposer_tools)).items():
                session = await tool_stack.enter_async_context(lifespan)
                if session is None or runtime.get_tool_resources(tool_type) is None:
                    raise RuntimeError("The native LeanSearch tool session is unavailable")
        with patch("ax_prover.utils.build._run_lean_subprocess", sandbox.compile):
            agent = await InstrumentedProver.create(config, runtime)
            if [tool.name for tool in agent.proposer_tools] != ["search_lean_search_tool"]:
                raise RuntimeError("The required native LeanSearch tool was not created")
            client = make_client(
                controller,
                settings,
                transcript_dir,
                candidate=latest_candidate,
                driver_state=driver,
            )
            agent.llm_client = client
            agent.memory.llm = client
            agent.summary_llm_client = client
            async with AsyncSqliteSaver.from_conn_string(
                str(controller.output_dir / "graph.sqlite")
            ) as saver:
                graph = agent.app.builder.compile(checkpointer=saver)
                graph_config = {
                    "configurable": {"thread_id": controller.state["run"]["run_id"]},
                    "recursion_limit": 2000,
                }
                if driver["graph_started"]:
                    initial = None
                else:
                    items = await parse_prove_target(
                        sandbox, str(workspace), f"{target}:{declaration}"
                    )
                    if len(items) != 1:
                        raise ValueError(
                            "The registered target must resolve to exactly one theorem"
                        )
                    initial = ProverAgentState(item=items[0])
                if not controller.state["snapshots"]:
                    controller.after_boundary(latest_candidate, driver, boundary="initial")
                if controller.state["paused"]:
                    return
                driver["graph_started"] = True
                atomic_json(controller.output_dir / "ax-driver.json", driver)
                async for values in graph.astream(
                    initial, config=graph_config, stream_mode="values"
                ):
                    state = ProverAgentState.model_validate(values)
                    atomic_json(
                        controller.output_dir / "ax-trajectory.json", state.model_dump(mode="json")
                    )
                    driver["lean_invocations"] = sandbox.invocations
                    driver["sandbox"] = sandbox.state()
                    checkpoint = await graph.aget_state(graph_config)
                    if checkpoint.next in {("proposer",), ("aggregate_metrics",)}:
                        controller.after_boundary(
                            latest_candidate, driver, boundary="refinement_cycle"
                        )
                    if controller.state["paused"]:
                        return
                if not controller.state["paused"]:
                    controller.after_boundary(
                        latest_candidate, driver, stop_reason="agent_exit", boundary="exit"
                    )
    except RunStopped:
        pass
    except ContainerCleanupError:
        raise
    except (Exception, NativeSearchIntegrityError) as error:
        driver["resumable"] = False
        atomic_json(
            controller.output_dir / "adapter-error.json",
            {"type": type(error).__name__, "error": str(error)},
        )
        if not controller.state["paused"]:
            reason = (
                "context_capacity"
                if "context" in type(error).__name__.lower()
                else "infrastructure_failure"
            )
            controller.after_boundary(
                latest_candidate, driver, stop_reason=reason, boundary="error"
            )
    finally:
        try:
            await tool_stack.aclose()
        finally:
            await sandbox.aclose()
