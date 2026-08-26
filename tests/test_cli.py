from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from minileanagent.cli import app

runner = CliRunner()


def test_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "mini-lean-agent" in result.stdout


def test_doctor_with_configured_command(lean_project: tuple[Path, str]) -> None:
    project, command = lean_project
    result = runner.invoke(
        app,
        ["doctor", "--project", str(project), "--lean-command", command],
    )
    assert result.exit_code == 0
    assert '"ready": true' in result.stdout


def test_check_command(lean_project: tuple[Path, str]) -> None:
    project, command = lean_project
    (project / "Main.lean").write_text("theorem ok : True := by trivial\n", encoding="utf-8")
    result = runner.invoke(
        app,
        [
            "check",
            "Main.lean",
            "--project",
            str(project),
            "--lean-command",
            command,
        ],
    )
    assert result.exit_code == 0
    assert '"success": true' in result.stdout


def test_inspect_command(tmp_path: Path) -> None:
    trajectory = tmp_path / "run.json"
    trajectory.write_text(
        json.dumps(
            {
                "run": {"status": "success", "model": "x", "steps": 1, "cost": 0},
                "environment": {"project": "/tmp/p", "toolset": "lean"},
                "messages": [{"role": "assistant", "content": "done"}],
            }
        ),
        encoding="utf-8",
    )
    result = runner.invoke(app, ["inspect", str(trajectory), "--full"])
    assert result.exit_code == 0
    assert "success" in result.stdout
    assert "done" in result.stdout


def test_solve_requires_model(lean_project: tuple[Path, str]) -> None:
    project, _ = lean_project
    result = runner.invoke(app, ["solve", "task", "--project", str(project)])
    assert result.exit_code == 2
    assert "No model configured" in result.stdout


def test_build_agent_and_solve_success(
    lean_project: tuple[Path, str], tmp_path: Path, monkeypatch
) -> None:
    from minileanagent import cli
    from minileanagent.agent import RunResult

    project, command = lean_project
    built = cli._build_agent(
        project=project,
        target=Path("Main.lean"),
        model_name="provider/model",
        toolset=cli.Toolset.bash,
        backend=cli.CheckerBackend.local,
        lean_command=command,
        kimina_url=None,
        kimina_api_key=None,
        api_base=None,
        api_key=None,
        temperature=0.0,
        max_tokens=None,
        step_limit=2,
        cost_limit=1.0,
        wall_time_limit=10,
        command_timeout=5,
        max_output_chars=1000,
        trajectory=tmp_path / "built.json",
    )
    assert built.environment.toolset == "bash"
    assert built.model.name == "provider/model"

    class FakeAgent:
        def run(self, task: str) -> RunResult:
            assert task == "prove it"
            return RunResult(
                status="success",
                submission="done",
                steps=2,
                cost=0.1,
                elapsed_seconds=1.0,
                trajectory_path=tmp_path / "run.json",
            )

    monkeypatch.setattr(cli, "_build_agent", lambda **kwargs: FakeAgent())
    result = runner.invoke(
        app,
        [
            "solve",
            "prove it",
            "--project",
            str(project),
            "--target",
            "Main.lean",
            "--model",
            "provider/model",
        ],
    )
    assert result.exit_code == 0
    assert "Security warning" in result.stdout
    assert "success" in result.stdout
    assert "done" in result.stdout


def test_batch_success_and_error(tmp_path: Path, monkeypatch) -> None:
    from minileanagent import cli
    from minileanagent.agent import RunResult

    project = tmp_path / "p"
    project.mkdir()
    tasks = tmp_path / "tasks.jsonl"
    tasks.write_text(
        "\n".join(
            [
                json.dumps({"id": "ok", "task": "one", "project": str(project)}),
                json.dumps({"id": "bad", "task": "two", "project": str(project)}),
            ]
        ),
        encoding="utf-8",
    )

    class FakeAgent:
        def __init__(self, fail: bool) -> None:
            self.fail = fail

        def run(self, task: str) -> RunResult:
            if self.fail:
                raise RuntimeError("synthetic")
            return RunResult("success", "ok", 1, 0.0, 0.1, None)

    calls = iter([FakeAgent(False), FakeAgent(True)])
    monkeypatch.setattr(cli, "_build_agent", lambda **kwargs: next(calls))
    output = tmp_path / "out"
    result = runner.invoke(
        app,
        [
            "batch",
            str(tasks),
            "--model",
            "provider/model",
            "--output-dir",
            str(output),
        ],
    )
    assert result.exit_code == 1
    rows = [json.loads(line) for line in (output / "results.jsonl").read_text().splitlines()]
    assert rows[0]["status"] == "success"
    assert rows[1]["status"] == "error"


def test_batch_rejects_invalid_json(tmp_path: Path) -> None:
    tasks = tmp_path / "tasks.jsonl"
    tasks.write_text("not json\n", encoding="utf-8")
    result = runner.invoke(app, ["batch", str(tasks), "--model", "provider/model"])
    assert result.exit_code != 0
    assert "Invalid JSON" in result.output


def test_batch_rejects_empty_file(tmp_path: Path) -> None:
    tasks = tmp_path / "tasks.jsonl"
    tasks.write_text("\n", encoding="utf-8")
    result = runner.invoke(app, ["batch", str(tasks), "--model", "provider/model"])
    assert result.exit_code != 0
    assert "no runnable" in result.output
