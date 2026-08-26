from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from minileanagent.agent import AgentConfig, MiniLeanAgent
from minileanagent.environment import LocalLeanEnvironment
from minileanagent.types import Message, ModelTurn, ToolCall, ToolSchema


@dataclass
class ScriptedModel:
    turns: list[ModelTurn]
    model_name: str = "scripted/model"

    @property
    def name(self) -> str:
        return self.model_name

    def query(self, messages: list[Message], tools: list[ToolSchema]) -> ModelTurn:
        assert messages
        assert tools
        return self.turns.pop(0)


def test_agent_success_and_linear_trajectory(
    lean_project: tuple[Path, str], tmp_path: Path
) -> None:
    project, command = lean_project
    target = project / "Main.lean"
    target.write_text("BROKEN\n", encoding="utf-8")
    trajectory = tmp_path / "trajectory.json"
    model = ScriptedModel(
        [
            ModelTurn(
                content="I will replace the placeholder.",
                tool_calls=(
                    ToolCall(
                        id="edit",
                        name="bash",
                        arguments={
                            "command": (
                                "cat > Main.lean <<'EOF'\n"
                                "theorem good : True := by trivial\n"
                                "EOF"
                            )
                        },
                    ),
                ),
                cost=0.01,
            ),
            ModelTurn(
                content="The proof is complete.",
                tool_calls=(
                    ToolCall(
                        id="submit",
                        name="bash",
                        arguments={"command": "mini-lean-submit Main.lean"},
                    ),
                ),
                cost=0.02,
            ),
        ]
    )
    environment = LocalLeanEnvironment(
        project, target="Main.lean", lean_command=command, toolset="lean"
    )
    agent = MiniLeanAgent(
        model,
        environment,
        AgentConfig(trajectory_path=trajectory, step_limit=5),
    )

    result = agent.run("Prove the theorem in Main.lean")

    assert result.status == "success"
    assert result.steps == 2
    assert result.cost == 0.03
    assert result.submission == "Verified Main.lean"
    data = json.loads(trajectory.read_text(encoding="utf-8"))
    assert data["trajectory_schema"] == "mini-lean-agent-0.1"
    assert [message["role"] for message in data["messages"]] == [
        "system",
        "user",
        "assistant",
        "tool",
        "assistant",
        "tool",
    ]
    assert data["run"]["status"] == "success"


def test_format_errors_stop_the_agent(lean_project: tuple[Path, str]) -> None:
    project, command = lean_project
    model = ScriptedModel([ModelTurn(content="No tool") for _ in range(3)])
    environment = LocalLeanEnvironment(project, lean_command=command)
    agent = MiniLeanAgent(
        model,
        environment,
        AgentConfig(max_consecutive_format_errors=3, step_limit=10),
    )

    result = agent.run("Do something")

    assert result.status == "format_error"
    assert result.steps == 3


def test_step_and_cost_limits(lean_project: tuple[Path, str]) -> None:
    project, command = lean_project
    environment = LocalLeanEnvironment(project, lean_command=command)
    bash_turn = ModelTurn(
        tool_calls=(ToolCall(id="ls", name="bash", arguments={"command": "true"}),),
        cost=0.25,
    )

    step_agent = MiniLeanAgent(
        ScriptedModel([bash_turn]),
        environment,
        AgentConfig(step_limit=1, cost_limit=0),
    )
    cost_agent = MiniLeanAgent(
        ScriptedModel([bash_turn]),
        environment,
        AgentConfig(step_limit=10, cost_limit=0.2),
    )

    assert step_agent.run("task").status == "step_limit"
    assert cost_agent.run("task").status == "cost_limit"


def test_time_limit_and_interrupt(lean_project: tuple[Path, str], monkeypatch) -> None:
    project, command = lean_project
    environment = LocalLeanEnvironment(project, lean_command=command)
    values = iter([0.0, 2.0, 2.0])
    monkeypatch.setattr("minileanagent.agent.time.monotonic", lambda: next(values))
    agent = MiniLeanAgent(
        ScriptedModel([]),
        environment,
        AgentConfig(wall_time_limit_seconds=1),
    )
    assert agent.run("task").status == "time_limit"
    monkeypatch.undo()

    class InterruptModel:
        name = "interrupt"

        def query(self, messages, tools):
            raise KeyboardInterrupt

    interrupted = MiniLeanAgent(InterruptModel(), environment, AgentConfig())
    assert interrupted.run("task").status == "interrupted"


def test_uncaught_model_error_is_saved(
    lean_project: tuple[Path, str], tmp_path: Path
) -> None:
    import pytest

    project, command = lean_project
    environment = LocalLeanEnvironment(project, lean_command=command)

    class BadModel:
        name = "bad"

        def query(self, messages, tools):
            raise RuntimeError("boom")

    trajectory = tmp_path / "error.json"
    agent = MiniLeanAgent(
        BadModel(),
        environment,
        AgentConfig(trajectory_path=trajectory),
    )
    with pytest.raises(RuntimeError, match="boom"):
        agent.run("task")
    data = json.loads(trajectory.read_text(encoding="utf-8"))
    assert data["run"]["status"] == "error"
    assert data["events"][-1]["exception_type"] == "RuntimeError"


def test_agent_config_validation() -> None:
    import pytest

    with pytest.raises(ValueError, match="step_limit"):
        AgentConfig(step_limit=-1)
    with pytest.raises(ValueError, match="cost_limit"):
        AgentConfig(cost_limit=-1)
    with pytest.raises(ValueError, match="wall_time"):
        AgentConfig(wall_time_limit_seconds=-1)
    with pytest.raises(ValueError, match="format_errors"):
        AgentConfig(max_consecutive_format_errors=0)


def test_bash_format_reprompt_does_not_advertise_lean_check(
    lean_project: tuple[Path, str],
) -> None:
    project, command = lean_project
    model = ScriptedModel([ModelTurn(content="No tool")])
    environment = LocalLeanEnvironment(project, lean_command=command, toolset="bash")
    agent = MiniLeanAgent(
        model,
        environment,
        AgentConfig(max_consecutive_format_errors=1, step_limit=2),
    )

    agent.run("task")

    assert "lean_check" not in str(agent.messages[-1]["content"])


def test_reusing_agent_resets_run_state(lean_project: tuple[Path, str]) -> None:
    project, command = lean_project
    (project / "Main.lean").write_text(
        "theorem good : True := by trivial\n", encoding="utf-8"
    )
    submit = ModelTurn(
        tool_calls=(
            ToolCall(
                id="submit",
                name="bash",
                arguments={"command": "mini-lean-submit"},
            ),
        ),
        cost=0.1,
    )
    model = ScriptedModel([submit, submit])
    environment = LocalLeanEnvironment(
        project, target="Main.lean", lean_command=command
    )
    agent = MiniLeanAgent(model, environment)

    first = agent.run("first")
    second = agent.run("second")

    assert first.status == second.status == "success"
    assert first.steps == second.steps == 1
    assert first.cost == second.cost == 0.1
    assert len(agent.events) == 2
