from __future__ import annotations

import json
import os
import shlex
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.integration

try:
    from minisweagent.agents.default import DefaultAgent
    from minisweagent.environments.local import LocalEnvironment
except ModuleNotFoundError:
    pytest.skip("mini-swe-agent is not installed", allow_module_level=True)


class ScriptedModel:
    """Minimal deterministic model implementing mini-SWE-agent's model protocol."""

    def __init__(self, actions: list[dict[str, str]]) -> None:
        self.actions = actions

    def format_message(self, **kwargs: Any) -> dict[str, Any]:
        return dict(kwargs)

    def query(self, messages: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
        del messages, kwargs
        command = self.actions.pop(0)
        return {
            "role": "assistant",
            "content": "working",
            "extra": {"actions": [command], "cost": 0.0},
        }

    def format_observation_messages(
        self,
        message: dict[str, Any],
        outputs: list[dict[str, Any]],
        template_vars: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        del message, template_vars
        return [{"role": "user", "content": json.dumps(outputs)}]

    def get_template_vars(self, **kwargs: Any) -> dict[str, Any]:
        return {}

    def serialize(self) -> dict[str, Any]:
        return {"info": {"config": {"model": {"name": "scripted"}}}}


def test_upstream_agent_loop_environment_and_trajectory_are_used(
    fake_lean_project: tuple[Path, str], tmp_path: Path
) -> None:
    project, command = fake_lean_project
    (project / "Main.lean").write_text(
        "theorem Demo.complete : True := by sorry\n",
        encoding="utf-8",
    )
    trajectory = tmp_path / "upstream.traj.json"
    submit = shlex.join([sys.executable, "-m", "minileanagent.submit"])
    model = ScriptedModel(
        [
            {
                "command": (
                    "cat > Main.lean <<'EOF'\ntheorem Demo.complete : True := by trivial\nEOF"
                )
            },
            {"command": submit},
        ]
    )
    source_root = Path(__file__).resolve().parents[1] / "src"
    pythonpath = os.pathsep.join(
        value for value in (str(source_root), os.environ.get("PYTHONPATH", "")) if value
    )
    environment = LocalEnvironment(
        cwd=str(project),
        env={
            "PYTHONPATH": pythonpath,
            "MINI_LEAN_PROJECT": str(project),
            "MINI_LEAN_TARGET": "Main.lean",
            "MINI_LEAN_COMMAND": command,
            "MINI_LEAN_VERIFY_TIMEOUT": "5",
        },
        timeout=10,
    )
    agent = DefaultAgent(
        model,
        environment,
        system_template="Lean system",
        instance_template="{{task}}",
        step_limit=5,
        cost_limit=0,
        output_path=trajectory,
    )
    result = agent.run("prove")
    assert result["exit_status"] == "Submitted"
    assert result["submission"].startswith("Verified Main.lean")

    data = json.loads(trajectory.read_text(encoding="utf-8"))
    assert data["trajectory_format"].startswith("mini-swe-agent")
    assert data["info"]["mini_version"]
    assert any(
        "Verified Main.lean" in str(message.get("content", ""))
        for message in data["messages"]
        if message.get("role") == "exit"
    )


def test_lean_config_overlays_upstream_mini_yaml() -> None:
    from minileanagent.config import load_config

    config = load_config()
    assert "mini-lean-submit" in config["agent"]["instance_template"]
    assert "observation_template" in config["model"]
    assert config["model"]["model_kwargs"]["drop_params"] is True
