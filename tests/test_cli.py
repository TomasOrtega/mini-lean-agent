from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from click import unstyle
from typer import rich_utils
from typer.testing import CliRunner

from minileanagent import cli
from minileanagent.cli import app

runner = CliRunner()


@pytest.fixture(params=[False, True], ids=["plain", "colored"])
def terminal_colors(request, monkeypatch) -> None:
    monkeypatch.setattr(rich_utils, "FORCE_TERMINAL", request.param)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm")


@pytest.mark.parametrize("command", ["solve", "check"])
def test_commands_require_verification_project(
    fake_lean_project: tuple[Path, str], monkeypatch, command: str, terminal_colors
) -> None:
    project, _ = fake_lean_project
    calls: list[object] = []

    def fake_call(*args, **kwargs):
        calls.append(args)
        return SimpleNamespace(to_dict=lambda: {"ok": True}, ok=True, verified=True)

    monkeypatch.setattr(cli, "run_task", fake_call)
    monkeypatch.setattr(cli, "verify_target", fake_call)
    result = runner.invoke(
        app,
        [
            command,
            "Main.lean",
            "--project",
            str(project),
            "--declaration",
            "Demo.complete",
            "--json",
        ],
    )
    assert result.exit_code == 2
    output = unstyle(result.output)
    assert "Missing option" in output
    assert "--verification-project" in output
    assert calls == []


def test_version_reports_both_projects(monkeypatch) -> None:
    monkeypatch.setattr(cli, "installed_upstream_version", lambda: "2.4.6")
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "mini-lean-agent" in result.stdout
    assert "mini-swe-agent 2.4.6" in result.stdout


@pytest.mark.parametrize("ok", [True, False])
def test_check_command(fake_lean_project: tuple[Path, str], monkeypatch, ok: bool) -> None:
    project, _ = fake_lean_project
    verifier = project.parent / "verifier"
    seen: dict[str, object] = {}

    def fake_verify(project_path, target, **kwargs):
        seen.update(kwargs)
        assert project_path == project
        assert target == Path("Main.lean")
        return SimpleNamespace(
            ok=ok, reason="comparison result", target=target, output="comparator details"
        )

    monkeypatch.setattr(cli, "verify_target", fake_verify)
    result = runner.invoke(
        app,
        [
            "check",
            "Main.lean",
            "--project",
            str(project),
            "--verification-project",
            str(verifier),
            "--declaration",
            "Demo.complete",
            "--comparator",
            "/tools/my comparator",
            "--timeout",
            "42",
        ],
    )
    assert result.exit_code == (0 if ok else 1)
    assert "comparison result: Main.lean" in result.stdout
    assert "comparator details" in result.stdout
    assert seen == {
        "verification_project": verifier,
        "declaration": "Demo.complete",
        "comparator": "/tools/my comparator",
        "timeout_seconds": 42,
    }


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
            "--verification-project",
            str(project.parent / "verifier"),
            "--comparator",
            "/tools/my comparator",
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
    assert seen["verification_project"] == project.parent / "verifier"
    assert seen["comparator"] == "/tools/my comparator"
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
        [
            "solve",
            "prove",
            "--project",
            str(project),
            "--target",
            "Main.lean",
            "--declaration",
            "Demo.complete",
            "--verification-project",
            str(project.parent / "verifier"),
        ],
    )
    assert result.exit_code == 1
    assert "verified: false" in result.stdout


@pytest.mark.parametrize("command", ["solve", "check"])
def test_commands_require_declaration(
    fake_lean_project: tuple[Path, str], monkeypatch, command: str, terminal_colors
) -> None:
    project, _ = fake_lean_project
    calls: list[str] = []

    def fake_run_task(task: str, **kwargs):
        calls.append(task)
        return SimpleNamespace(to_dict=lambda: {"verified": True}, verified=True)

    monkeypatch.setattr(cli, "run_task", fake_run_task)
    monkeypatch.setattr(cli, "verify_target", fake_run_task)
    result = runner.invoke(
        app,
        [
            command,
            "Main.lean",
            "--project",
            str(project),
            "--verification-project",
            str(project.parent / "verifier"),
            "--json",
        ],
    )
    assert result.exit_code == 2
    output = unstyle(result.output)
    assert "Missing option" in output
    assert "--declaration" in output
    assert calls == []


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
            "--declaration",
            "Demo.complete",
            "--verification-project",
            str(project.parent / "verifier"),
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


def test_check_json_output(fake_lean_project: tuple[Path, str], monkeypatch) -> None:
    project, _ = fake_lean_project

    def fake_verify(*args, **kwargs):
        assert kwargs["comparator"] == "comparator"
        return SimpleNamespace(
            ok=True,
            to_dict=lambda: {"ok": True, "verification_project": str(project.parent / "verifier")},
        )

    monkeypatch.setattr(cli, "verify_target", fake_verify)
    result = runner.invoke(
        app,
        [
            "check",
            "Main.lean",
            "--project",
            str(project),
            "--declaration",
            "Demo.complete",
            "--verification-project",
            str(project.parent / "verifier"),
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
            "--declaration",
            "Demo.complete",
            "--verification-project",
            str(project.parent / "verifier"),
            "--model-config",
            "{",
        ],
    )
    assert result.exit_code == 2
    assert "Invalid JSON" in result.output


def test_check_invalid_target_is_usage_error(tmp_path: Path, monkeypatch) -> None:
    project = tmp_path / "project"
    project.mkdir()

    def fake_verify(*args, **kwargs):
        raise ValueError("Target does not exist")

    monkeypatch.setattr(cli, "verify_target", fake_verify)
    result = runner.invoke(
        app,
        [
            "check",
            "Missing.lean",
            "--project",
            str(project),
            "--declaration",
            "Demo.complete",
            "--verification-project",
            str(tmp_path / "verifier"),
        ],
    )
    assert result.exit_code == 2
    assert "does not exist" in result.output


def test_batch_command_delegates_comparator(tmp_path: Path, monkeypatch) -> None:
    tasks = tmp_path / "tasks.jsonl"
    seen: dict[str, object] = {}

    def fake_run_batch(tasks_path, **kwargs):
        assert tasks_path == tasks
        seen.update(kwargs)
        return [{"verified": True}]

    monkeypatch.setattr(cli, "run_batch", fake_run_batch)
    result = runner.invoke(app, ["batch", str(tasks), "--comparator", "/tools/my comparator"])
    assert result.exit_code == 0
    assert seen["comparator"] == "/tools/my comparator"
    assert "completed: 1" in result.output
