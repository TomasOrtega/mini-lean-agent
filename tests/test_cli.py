from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from typer.testing import CliRunner

from minileanagent import cli
from minileanagent.cli import app

runner = CliRunner()


def test_version_reports_both_projects(monkeypatch) -> None:
    monkeypatch.setattr(cli, "installed_upstream_version", lambda: "2.4.6")
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "mini-lean-agent" in result.stdout
    assert "mini-swe-agent 2.4.6" in result.stdout


def test_check_command(fake_lean_project: tuple[Path, str]) -> None:
    project, command = fake_lean_project
    result = runner.invoke(
        app,
        ["check", "Main.lean", "--project", str(project), "--lean-command", command],
    )
    assert result.exit_code == 0
    assert "accepted" in result.stdout

    (project / "Main.lean").write_text(
        "theorem Demo.bad : True := by sorry\n",
        encoding="utf-8",
    )
    rejected = runner.invoke(
        app,
        ["check", "Main.lean", "--project", str(project), "--lean-command", command],
    )
    assert rejected.exit_code == 1
    assert "placeholder" in rejected.stdout


def test_solve_delegates_to_runner(
    fake_lean_project: tuple[Path, str], tmp_path: Path, monkeypatch
) -> None:
    project, command = fake_lean_project
    seen: dict[str, object] = {}

    class FakeOutcome:
        status = "success"
        verified = True
        upstream_exit_status = "Submitted"
        trajectory_path = tmp_path / "run.traj.json"
        verification = SimpleNamespace(reason="accepted")

        def to_dict(self):
            return {"status": self.status, "verified": self.verified}

    def fake_run_task(task: str, **kwargs):
        seen["task"] = task
        seen.update(kwargs)
        return FakeOutcome()

    monkeypatch.setattr(cli, "run_task", fake_run_task)
    result = runner.invoke(
        app,
        [
            "solve",
            "replace the sorry",
            "--project",
            str(project),
            "--target",
            "Main.lean",
            "--declaration",
            "Demo.complete",
            "--model",
            "provider/model",
            "--lean-command",
            command,
            "--model-config",
            '{"model_kwargs": {"temperature": 0}}',
        ],
    )
    assert result.exit_code == 0
    assert seen["task"] == "replace the sorry"
    assert seen["declaration"] == "Demo.complete"
    assert seen["model_name"] == "provider/model"
    assert seen["model_config"] == {"model_kwargs": {"temperature": 0}}
    assert "verified: true" in result.stdout


def test_solve_returns_failure_for_unverified_result(
    fake_lean_project: tuple[Path, str], monkeypatch
) -> None:
    project, _ = fake_lean_project
    outcome = SimpleNamespace(
        status="verification_failed",
        verified=False,
        upstream_exit_status="Submitted",
        trajectory_path=project / "run.json",
        verification=SimpleNamespace(reason="contains sorry"),
        to_dict=lambda: {"verified": False},
    )
    monkeypatch.setattr(cli, "run_task", lambda *args, **kwargs: outcome)
    result = runner.invoke(
        app,
        ["solve", "prove", "--project", str(project), "--target", "Main.lean"],
    )
    assert result.exit_code == 1
    assert "verified: false" in result.stdout


def test_model_config_must_be_object(fake_lean_project: tuple[Path, str]) -> None:
    project, _ = fake_lean_project
    result = runner.invoke(
        app,
        [
            "solve",
            "prove",
            "--project",
            str(project),
            "--target",
            "Main.lean",
            "--model-config",
            "[]",
        ],
    )
    assert result.exit_code == 2
    assert "Expected a JSON object" in result.output


def test_doctor_resolves_command(fake_lean_project: tuple[Path, str], monkeypatch) -> None:
    project, command = fake_lean_project
    monkeypatch.setattr(cli, "installed_upstream_version", lambda: "2.4.6")
    result = runner.invoke(
        app,
        [
            "doctor",
            "--project",
            str(project),
            "--target",
            "Main.lean",
            "--lean-command",
            command,
        ],
    )
    assert result.exit_code == 0
    assert "mini-swe-agent: 2.4.6" in result.stdout
    assert "command:" in result.stdout


def test_check_json_output(fake_lean_project: tuple[Path, str]) -> None:
    project, command = fake_lean_project
    result = runner.invoke(
        app,
        [
            "check",
            "Main.lean",
            "--project",
            str(project),
            "--lean-command",
            command,
            "--json",
        ],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True


def test_root_version_flag(monkeypatch) -> None:
    monkeypatch.setattr(cli, "installed_upstream_version", lambda: "2.4.6")
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "mini-lean-agent" in result.stdout
    assert "2.4.6" in result.stdout


def test_config_path_command() -> None:
    result = runner.invoke(app, ["config-path"])
    assert result.exit_code == 0
    assert result.stdout.strip().endswith("lean.yaml")


def test_model_config_invalid_json(fake_lean_project: tuple[Path, str]) -> None:
    project, _ = fake_lean_project
    result = runner.invoke(
        app,
        [
            "solve",
            "prove",
            "--project",
            str(project),
            "--model-config",
            "{",
        ],
    )
    assert result.exit_code == 2
    assert "Invalid JSON" in result.output


def test_check_invalid_target_is_usage_error(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    result = runner.invoke(
        app,
        ["check", "Missing.lean", "--project", str(project)],
    )
    assert result.exit_code == 2
    assert "does not exist" in result.output
