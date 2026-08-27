from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from minileanagent import runner
from minileanagent.runner import RunOutcome, UpstreamAPI


def fake_lean_config() -> dict[str, Any]:
    """Representative upstream mini.yaml merged with the Lean overlay."""
    return {
        "agent": {
            "system_template": "Lean system",
            "instance_template": "{{task}} mini-lean-submit",
            "step_limit": 30,
            "cost_limit": 3.0,
            "wall_time_limit_seconds": 900,
        },
        "environment": {"env": {"PAGER": "cat"}},
        "model": {"model_kwargs": {"drop_params": True}},
    }


class FakeEnvironment:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs


class FakeAgent:
    edit_target = True

    def __init__(self, model: Any, environment: FakeEnvironment, **kwargs: Any) -> None:
        self.model = model
        self.environment = environment
        self.kwargs = kwargs
        self.extra_template_vars: dict[str, Any] = {}
        self.cost = 0.25
        self.n_calls = 2
        self.messages: list[dict[str, Any]] = []

    def run(self, task: str) -> dict[str, Any]:
        self.messages.append({"role": "user", "content": task})
        if self.edit_target:
            project = Path(self.environment.kwargs["cwd"])
            target = Path(self.environment.kwargs["env"]["MINI_LEAN_TARGET"])
            (project / target).write_text(
                "theorem Demo.complete : True := by trivial\n",
                encoding="utf-8",
            )
        result = {"exit_status": "Submitted", "submission": "verified"}
        self.save(self.kwargs["output_path"])
        return result

    def serialize(self, *extra: dict[str, Any]) -> dict[str, Any]:
        data: dict[str, Any] = {
            "info": {"mini_version": "2.4.6"},
            "messages": self.messages,
            "trajectory_format": "mini-swe-agent-1.1",
        }
        for addition in extra:
            _deep_merge(data, addition)
        return data

    def save(self, path: Path, *extra: dict[str, Any]) -> dict[str, Any]:
        data = self.serialize(*extra)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
        return data


def _deep_merge(target: dict[str, Any], addition: dict[str, Any]) -> None:
    for key, value in addition.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_merge(target[key], value)
        else:
            target[key] = value


def fake_upstream(seen: dict[str, Any], agent_class: type[FakeAgent] = FakeAgent) -> UpstreamAPI:
    def get_model(model_name: str | None, config: dict[str, Any]) -> dict[str, Any]:
        seen["model_name"] = model_name
        seen["model_config"] = config
        return {"name": model_name, "config": config}

    class RecordingEnvironment(FakeEnvironment):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            seen["environment"] = kwargs

    class RecordingAgent(agent_class):
        def __init__(self, model: Any, environment: FakeEnvironment, **kwargs: Any) -> None:
            super().__init__(model, environment, **kwargs)
            seen["agent"] = kwargs
            seen["agent_object"] = self

    return UpstreamAPI(
        default_agent=RecordingAgent,
        local_environment=RecordingEnvironment,
        get_model=get_model,
        version="2.4.6",
    )


def test_build_agent_is_thin_upstream_construction(
    fake_lean_project: tuple[Path, str], tmp_path: Path, monkeypatch
) -> None:
    project, command = fake_lean_project
    seen: dict[str, Any] = {}
    monkeypatch.setattr(runner, "_load_upstream", lambda: fake_upstream(seen))
    monkeypatch.setattr(runner, "load_config", lambda _: fake_lean_config())

    trajectory = tmp_path / "run.traj.json"
    agent = runner.build_agent(
        project=project,
        target="Main.lean",
        model_name="provider/model",
        trajectory_path=trajectory,
        declaration="Demo.complete",
        expected_type="Demo.complete : True",
        lean_command=command,
        model_config={"model_kwargs": {"temperature": 0}},
    )

    assert agent is seen["agent_object"]
    assert seen["model_name"] == "provider/model"
    assert seen["model_config"]["model_kwargs"] == {
        "drop_params": True,
        "temperature": 0,
    }
    environment = seen["environment"]
    assert environment["cwd"] == str(project)
    assert environment["env"]["MINI_LEAN_TARGET"] == "Main.lean"
    assert environment["env"]["MINI_LEAN_DECLARATION"] == "Demo.complete"
    assert seen["agent"]["output_path"] == trajectory
    assert "mini-lean-submit" in seen["agent"]["instance_template"]
    assert agent.extra_template_vars["lean_target"] == "Main.lean"
    assert agent.extra_template_vars["lean_statement_guarded"] is True


def test_run_task_preserves_upstream_trajectory_and_adds_metadata(
    fake_lean_project: tuple[Path, str], tmp_path: Path, monkeypatch
) -> None:
    project, command = fake_lean_project
    (project / "Main.lean").write_text(
        "theorem Demo.complete : True := by sorry\n",
        encoding="utf-8",
    )
    seen: dict[str, Any] = {}
    monkeypatch.setattr(runner, "_load_upstream", lambda: fake_upstream(seen))
    monkeypatch.setattr(runner, "load_config", lambda _: fake_lean_config())
    monkeypatch.setattr(runner, "installed_upstream_version", lambda: "2.4.6")
    trajectory = tmp_path / "result.traj.json"

    outcome = runner.run_task(
        "replace the sorry",
        project=project,
        target="Main.lean",
        declaration="Demo.complete",
        model_name="provider/model",
        trajectory_path=trajectory,
        lean_command=command,
    )

    assert outcome.status == "success"
    assert outcome.verified is True
    assert outcome.upstream_exit_status == "Submitted"
    assert outcome.verification.statement_preserved is True
    assert outcome.model_cost == 0.25
    assert outcome.model_calls == 2

    data = json.loads(trajectory.read_text(encoding="utf-8"))
    assert data["trajectory_format"] == "mini-swe-agent-1.1"
    metadata = data["info"]["mini_lean_agent"]
    assert metadata["upstream_version"] == "2.4.6"
    assert metadata["target"] == "Main.lean"
    assert metadata["verified"] is True


def test_direct_upstream_submission_does_not_bypass_evaluator(
    fake_lean_project: tuple[Path, str], tmp_path: Path, monkeypatch
) -> None:
    project, command = fake_lean_project
    (project / "Main.lean").write_text(
        "theorem Demo.complete : True := by sorry\n",
        encoding="utf-8",
    )

    class NoEditAgent(FakeAgent):
        edit_target = False

    seen: dict[str, Any] = {}
    monkeypatch.setattr(
        runner,
        "_load_upstream",
        lambda: fake_upstream(seen, agent_class=NoEditAgent),
    )
    monkeypatch.setattr(runner, "installed_upstream_version", lambda: "2.4.6")
    monkeypatch.setattr(runner, "load_config", lambda _: fake_lean_config())

    outcome = runner.run_task(
        "cheat by submitting immediately",
        project=project,
        target="Main.lean",
        model_name="provider/model",
        trajectory_path=tmp_path / "bypass.traj.json",
        lean_command=command,
    )
    assert outcome.upstream_exit_status == "Submitted"
    assert outcome.verified is False
    assert outcome.status == "verification_failed"


def test_limits_are_validated(fake_lean_project: tuple[Path, str], tmp_path: Path) -> None:
    project, _ = fake_lean_project
    with pytest.raises(ValueError, match="step_limit"):
        runner.build_agent(
            project=project,
            target="Main.lean",
            model_name="model",
            trajectory_path=tmp_path / "x.json",
            step_limit=-1,
        )


def test_run_outcome_serialization(fake_lean_project: tuple[Path, str]) -> None:
    project, command = fake_lean_project
    verification = runner.verify_target(project, "Main.lean", command_template=command)
    outcome = RunOutcome(
        status="success",
        verified=True,
        trajectory_path=project / "run.json",
        upstream_exit_status="Submitted",
        upstream_submission="ok",
        upstream_result={"exit_status": "Submitted"},
        verification=verification,
        expected_type=None,
        model_cost=0.1,
        model_calls=1,
    )
    assert outcome.to_dict()["verification"]["ok"] is True


def test_installed_upstream_version_is_safe_without_distribution(monkeypatch) -> None:
    def missing(_: str) -> str:
        raise runner.PackageNotFoundError

    monkeypatch.setattr(runner, "version", missing)
    assert runner.installed_upstream_version() == "not installed"


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"cost_limit": -1.0}, "cost_limit"),
        ({"wall_time_limit_seconds": -1}, "wall_time_limit_seconds"),
        ({"command_timeout_seconds": 0}, "command_timeout_seconds"),
        ({"verification_timeout_seconds": 0}, "verification_timeout_seconds"),
    ],
)
def test_all_limits_are_validated(
    fake_lean_project: tuple[Path, str],
    tmp_path: Path,
    kwargs: dict[str, object],
    message: str,
) -> None:
    project, _ = fake_lean_project
    with pytest.raises(ValueError, match=message):
        runner.build_agent(
            project=project,
            target="Main.lean",
            model_name="model",
            trajectory_path=tmp_path / "x.json",
            **kwargs,
        )


def test_status_mapping() -> None:
    assert runner._status_from_results(True, "LimitsExceeded") == "success"
    assert runner._status_from_results(False, "Submitted") == "verification_failed"
    assert runner._status_from_results(False, "TimeExceeded") == "TimeExceeded"
    assert runner._status_from_results(False, "") == "verification_failed"
