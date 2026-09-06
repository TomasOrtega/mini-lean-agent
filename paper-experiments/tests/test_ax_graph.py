import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import ClassVar

import pytest
from aiohttp import web
from ax_prover.models.files import Location
from ax_prover.models.messages import BuildSuccessFeedback
from ax_prover.models.proving import TargetItem
from langchain_core.messages import AIMessage

from paper_experiments.ax_adapter import run_axprover
from paper_experiments.controller import RunController
from paper_experiments.ledger import Ledger, PriceTable
from paper_experiments.protocol import register_runs


@pytest.fixture
async def leansearch_server(monkeypatch):
    from ax_prover.tools.registry import TOOL_REGISTRY

    from paper_experiments import retrieval

    requests = []
    sessions = []
    result = {"name": ["Eq", "refl"], "kind": "theorem", "signature": "a = a"}

    async def search(request):
        payload = await request.json()
        requests.append(payload)
        if result.get("redirect") and payload["query"] == ["rfl"]:
            return web.Response(status=302, headers={"Location": "https://example.com/forbidden"})
        return web.json_response([[{"result": result}]])

    app = web.Application()
    app.router.add_post("/search", search)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    server_url = f"http://127.0.0.1:{port}"
    monkeypatch.setattr(retrieval, "SERVER_URL", server_url, raising=False)
    monkeypatch.setattr(retrieval, "SEARCH_URL", server_url + "/search", raising=False)

    async def validated(record):
        pass

    monkeypatch.setattr(retrieval, "validate_retrieval_live", validated, raising=False)
    registration = TOOL_REGISTRY["search_lean_search"]
    original = registration.lifespan

    @asynccontextmanager
    async def lifespan(config):
        async with original(config) as session:
            sessions.append(session)
            yield session

    monkeypatch.setattr(registration, "lifespan", lifespan)
    try:
        yield {
            "tool_config": {
                "tool_type": "search_lean_search",
                "server_url": server_url,
                "max_results": 6,
                "timeout": 2,
                "max_retries": 1,
                "retry_delay": 0,
            },
            "requests": requests,
            "sessions": sessions,
            "result": result,
        }
    finally:
        await runner.cleanup()


class FakeSandbox:
    created: ClassVar[list] = []
    closed: ClassVar[int] = 0

    def __init__(
        self, workspace, image, output_dir, *, resume=None, resources=None, dependency_cache=None
    ):
        self.invocations = 0
        self.created.append(resume)

    def state(self):
        return {"container_id": "preserved", "invocations": self.invocations}

    async def compile(self, *args):
        return 0, "", ""

    async def aclose(self):
        type(self).closed += 1


@pytest.mark.parametrize(
    "scenario",
    [
        "success",
        "missing_tool",
        "continuation",
        "missing_archive",
        "corrupt_archive",
        "redirect",
        "redirect_at_limit",
    ],
)
async def test_native_ax_graph_retrieval_proposal_and_reviewer_share_ledger(
    tmp_path, monkeypatch, leansearch_server, scenario
):
    monkeypatch.setenv("GOOGLE_API_KEY", "test-not-a-key")
    monkeypatch.setattr("paper_experiments.ax_adapter.LeanSandbox", FakeSandbox)
    continuation = scenario in {"continuation", "missing_archive", "corrupt_archive"}
    closed_before = FakeSandbox.closed
    if scenario.startswith("redirect"):
        leansearch_server["result"]["redirect"] = True
    if scenario == "missing_tool":
        from ax_prover.tools.registry import TOOL_REGISTRY

        async def missing_tool(config, runtime):
            return None

        monkeypatch.setattr(TOOL_REGISTRY["search_lean_search"], "factory", missing_tool)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    original = "theorem smoke : True := by sorry\n"
    (workspace / "Target.lean").write_text(original)

    async def targets(*args):
        return [
            TargetItem(
                location=Location(module_path="Target", name="smoke"), original_source=original
            )
        ]

    async def builder(self, state):
        return {"messages": [BuildSuccessFeedback()]}

    monkeypatch.setattr("ax_prover.utils.proving.parse_prove_target", targets)
    monkeypatch.setattr("ax_prover.prover.agent.ProverAgent._builder_node", builder)
    provider_requests = []

    async def provider(self, messages, **kwargs):
        provider_requests.append(messages)
        usage = {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110}
        if len(provider_requests) == 1:
            from ax_prover.tools.lean_search import SearchQueryInput

            native_tools = kwargs["tools"]
            assert [tool.name for tool in native_tools] == ["search_lean_search_tool"]
            assert native_tools[0].args_schema is SearchQueryInput
            return AIMessage(
                content="",
                tool_calls=[
                    {"name": "search_lean_search_tool", "args": {"query": "rfl"}, "id": "one"}
                ],
                usage_metadata=usage,
                additional_kwargs={"thought_signature": "keep-me"},
            )
        if len(provider_requests) == 2:
            assert messages[-2].additional_kwargs["thought_signature"] == "keep-me"
            assert messages[-1].type == "tool"
            assert "Eq.refl [theorem]" in messages[-1].content
            assert "a = a" in messages[-1].content
            return AIMessage(
                content=json.dumps(
                    {
                        "imports": [],
                        "opens": [],
                        "updated_theorem": "theorem smoke : True := by trivial\n",
                    }
                ),
                usage_metadata=usage,
            )
        return AIMessage(
            content=json.dumps(
                {"check_1": True, "check_2": True, "check_3": True, "approved": True}
            ),
            usage_metadata=usage,
        )

    monkeypatch.setattr("ax_prover.utils.llm.LLMClient.ainvoke", provider)
    run = next(
        run
        for run in register_runs(0)["runs"]
        if run["harness"] == "axprover" and run["extendable"]
    )
    prices = PriceTable(
        "2026-09-04",
        "test",
        {
            run["model"]: {
                key: "20000" if continuation or scenario == "redirect_at_limit" else "1"
                for key in ("input", "output", "reasoning", "cache_read", "cache_write")
            }
        },
    )
    output = tmp_path / "output"
    controller = RunController(
        run,
        output,
        Ledger(output / "ledger.jsonl", prices),
        lambda candidate: {"verified": "trivial" in candidate.read_text()},
    )
    settings = {
        "provider": "google",
        "ax_model": "google_genai:gemini-3.8-flash",
        "requested_model": "gemini-3.8-flash",
        "thinking": {"level": "high"},
        "temperature": 1,
        "max_output_tokens": 32768,
        "request_timeout_seconds": 600,
        "max_attempts": 3,
    }
    args = {
        "workspace": workspace,
        "target": Path("Target.lean"),
        "declaration": "smoke",
        "task": "Prove it",
        "settings": settings,
        "image": "sha256:" + "a" * 64,
        "retrieval": {"tool_config": leansearch_server["tool_config"]},
    }
    await run_axprover(controller, **args)
    assert FakeSandbox.closed == closed_before + 1
    assert len(leansearch_server["sessions"]) == 1
    assert leansearch_server["sessions"][0].closed
    if continuation:
        assert controller.state["endpoint"]["stop_reason"] == "cost"
        assert controller.ledger.calls == 1
        leansearch_server["result"]["signature"] = "different future search output"
        frozen = (output / "low-endpoint.json").read_bytes()
        scoped_record = next(
            path
            for path in (output / "leansearch-requests").glob("*.json")
            if json.loads(path.read_text())["scope"] is not None
        )
        if scenario == "missing_archive":
            scoped_record.unlink()
        elif scenario == "corrupt_archive":
            scoped_record.write_text("{")
        state = controller.resume_extended()
        await run_axprover(controller, **args, resume_state=state)
        assert (output / "low-endpoint.json").read_bytes() == frozen
        assert len(leansearch_server["sessions"]) == 2
        assert leansearch_server["sessions"][1].closed
        assert FakeSandbox.closed == closed_before + 2
    error = (
        (output / "adapter-error.json").read_text()
        if (output / "adapter-error.json").exists()
        else ""
    )
    if scenario == "missing_tool":
        assert controller.state["endpoint"]["stop_reason"] == "infrastructure_failure"
        assert controller.ledger.calls == 0
        assert not provider_requests
        assert "native LeanSearch tool" in error
        assert leansearch_server["requests"] == [{"query": ["Nat"], "num_results": 6}]
        return
    if scenario in {"missing_archive", "corrupt_archive"}:
        assert controller.state["endpoint"]["stop_reason"] == "infrastructure_failure", error
        assert not controller.state["endpoint"]["resumable"]
        assert controller.ledger.calls == len(provider_requests) == 1
        assert "LeanSearch replay record" in error
        assert leansearch_server["requests"] == [
            {"query": ["Nat"], "num_results": 6},
            {"query": ["rfl"], "num_results": 6},
            {"query": ["Nat"], "num_results": 6},
        ]
        return
    if scenario.startswith("redirect"):
        assert not controller.state["endpoint"]["resumable"]
        assert controller.state["endpoint"]["stop_reason"] == (
            "cost" if scenario == "redirect_at_limit" else "infrastructure_failure"
        )
        assert controller.ledger.calls == len(provider_requests) == 1
        assert "redirects are forbidden" in error
        with pytest.raises(ValueError, match="technically resumable"):
            controller.resume_extended()
        return
    assert controller.state["endpoint"]["verified"], error
    assert controller.ledger.calls == 3
    assert [attempt["role"] for attempt in controller.ledger.snapshot()["attempts"]] == [
        "proposer",
        "proposer",
        "reviewer",
    ]
    assert "trivial" in (workspace / "Target.lean").read_text()
    assert (output / "graph.sqlite").exists()
    expected_requests = [
        {"query": ["Nat"], "num_results": 6},
        {"query": ["rfl"], "num_results": 6},
    ]
    if scenario == "continuation":
        expected_requests.append({"query": ["Nat"], "num_results": 6})
    assert leansearch_server["requests"] == expected_requests


async def test_ax_continuation_preserves_graph_memory_and_frozen_low(
    tmp_path, monkeypatch, leansearch_server
):
    monkeypatch.setenv("GOOGLE_API_KEY", "test-not-a-key")
    FakeSandbox.created = []
    monkeypatch.setattr("paper_experiments.ax_adapter.LeanSandbox", FakeSandbox)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    original = "theorem smoke : True := by sorry\n"
    (workspace / "Target.lean").write_text(original)

    async def targets(*args):
        return [
            TargetItem(
                location=Location(module_path="Target", name="smoke"), original_source=original
            )
        ]

    async def builder(self, state):
        return {"messages": [BuildSuccessFeedback()]}

    monkeypatch.setattr("ax_prover.utils.proving.parse_prove_target", targets)
    monkeypatch.setattr("ax_prover.prover.agent.ProverAgent._builder_node", builder)
    requests = []

    async def provider(self, messages, **kwargs):
        requests.append(messages)
        index = len(requests)
        if index in {1, 4}:
            if index == 4:
                assert "Remember the prior attempt" in str(messages)
            content = json.dumps(
                {
                    "imports": [],
                    "opens": [],
                    "updated_theorem": "theorem smoke : True := by "
                    + ("trivial" if index == 4 else "exact True.intro")
                    + "\n",
                }
            )
        elif index == 3:
            content = "Remember the prior attempt"
        else:
            content = json.dumps(
                {
                    "check_1": index == 5,
                    "check_2": index == 5,
                    "check_3": True,
                    "approved": index == 5,
                }
            )
        return AIMessage(
            content=content,
            usage_metadata={"input_tokens": 1, "output_tokens": 0, "total_tokens": 1},
        )

    monkeypatch.setattr("ax_prover.utils.llm.LLMClient.ainvoke", provider)
    run = next(
        r for r in register_runs(0)["runs"] if r["harness"] == "axprover" and r["extendable"]
    )
    prices = PriceTable(
        "2026-09-04",
        "test",
        {
            run["model"]: {
                key: "1000000"
                for key in ("input", "output", "reasoning", "cache_read", "cache_write")
            }
        },
    )
    output = tmp_path / "output"
    ledger = Ledger(output / "ledger.jsonl", prices)

    def evaluator(candidate):
        return {"verified": "trivial" in candidate.read_text()}

    controller = RunController(run, output, ledger, evaluator)
    settings = {
        "provider": "google",
        "ax_model": "google_genai:gemini-3.8-flash",
        "requested_model": "gemini-3.8-flash",
        "thinking": {"level": "high"},
        "temperature": 1,
        "max_output_tokens": 32768,
        "request_timeout_seconds": 600,
        "max_attempts": 3,
    }
    args = {
        "workspace": workspace,
        "target": Path("Target.lean"),
        "declaration": "smoke",
        "task": "Prove it",
        "settings": settings,
        "image": "sha256:" + "a" * 64,
        "retrieval": {"tool_config": leansearch_server["tool_config"]},
    }
    await run_axprover(controller, **args)
    assert controller.state["endpoint"]["stop_reason"] == "cost"
    assert not controller.state["endpoint"]["verified"]
    assert ledger.calls == 2
    frozen = (output / "low-endpoint.json").read_bytes()
    recovered = RunController(run, output, ledger, evaluator)
    state = recovered.resume_extended()
    await run_axprover(recovered, **args, resume_state=state)
    error = (
        (output / "adapter-error.json").read_text()
        if (output / "adapter-error.json").exists()
        else ""
    )
    assert recovered.state["endpoint"]["verified"], error
    assert ledger.calls == 5
    assert (output / "low-endpoint.json").read_bytes() == frozen
    assert recovered.checkpoint("2", panel="low") is False
    assert FakeSandbox.created == [None, {"container_id": "preserved", "invocations": 0}]
    assert len(leansearch_server["sessions"]) == 2
    assert all(session.closed for session in leansearch_server["sessions"])
