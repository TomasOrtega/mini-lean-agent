from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from minileanagent import runner

pytestmark = pytest.mark.integration

try:
    from minisweagent import __version__ as upstream_version
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


@pytest.mark.parametrize(
    ("replacement", "verified"),
    [
        (
            "theorem Demo.complete : True := by trivial\n",
            True,
        ),
        (None, False),
        (
            "-- TYPE_FALSE\ntheorem Demo.complete : False := by trivial\n",
            False,
        ),
    ],
    ids=["valid-proof", "early-submission", "changed-statement"],
)
def test_upstream_submission_is_independently_verified(
    fake_lean_project: tuple[Path, str],
    fake_comparator: tuple[Path, Path],
    tmp_path: Path,
    monkeypatch,
    replacement: str | None,
    verified: bool,
) -> None:
    project, command = fake_lean_project
    verifier, comparator = fake_comparator
    (project / "Main.lean").write_text(
        "theorem Demo.complete : True := by sorry\n",
        encoding="utf-8",
    )
    trajectory = tmp_path / "upstream.traj.json"
    actions = []
    if replacement is not None:
        actions.append({"command": f"cat > Main.lean <<'EOF'\n{replacement}EOF"})
    actions.append({"command": "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"})
    expected_calls = len(actions)
    model = ScriptedModel(actions)
    monkeypatch.setattr(
        runner,
        "_load_upstream",
        lambda: runner.UpstreamAPI(
            default_agent=DefaultAgent,
            local_environment=LocalEnvironment,
            get_model=lambda model_name, config: model,
            version=upstream_version,
        ),
    )

    outcome = runner.run_task(
        "Replace the sorry with a proof.",
        project=project,
        target="Main.lean",
        declaration="Demo.complete",
        verification_project=verifier,
        comparator=str(comparator),
        trajectory_path=trajectory,
        lean_command=command,
        step_limit=5,
        cost_limit=0,
        command_timeout_seconds=10,
        verification_timeout_seconds=5,
    )
    assert outcome.upstream_exit_status == "Submitted"
    assert outcome.upstream_submission == ""
    assert outcome.verified is verified
    assert outcome.status == ("success" if verified else "verification_failed")
    assert outcome.verification.ok is verified
    assert outcome.model_calls == expected_calls

    data = json.loads(trajectory.read_text(encoding="utf-8"))
    assert data["trajectory_format"].startswith("mini-swe-agent")
    assert data["info"]["mini_version"]
    assert data["info"]["config"]["agent_type"] == "minisweagent.agents.default.DefaultAgent"
    assert (
        data["info"]["config"]["environment_type"]
        == "minisweagent.environments.local.LocalEnvironment"
    )
    assert data["info"]["exit_status"] == "Submitted"
    assert data["info"]["mini_lean_agent"]["verified"] is verified
    assert data["info"]["mini_lean_agent"]["status"] == outcome.status
    assert data["messages"][-1]["role"] == "exit"
    assert data["messages"][-1]["extra"]["exit_status"] == "Submitted"


def test_lean_config_overlays_upstream_mini_yaml() -> None:
    from minisweagent.config import get_config_from_spec

    from minileanagent.config import load_config

    config = load_config()
    upstream_config = get_config_from_spec("mini.yaml")
    assert "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT" in config["agent"]["instance_template"]
    assert (
        config["model"]["format_error_template"]
        == upstream_config["model"]["format_error_template"]
    )
    assert "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT" in config["model"]["format_error_template"]
    assert "observation_template" in config["model"]
    assert config["model"]["model_kwargs"]["drop_params"] is True
