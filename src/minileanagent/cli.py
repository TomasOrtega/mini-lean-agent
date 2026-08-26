"""Command-line interface for mini-lean-agent."""

from __future__ import annotations

import json
import re
from dataclasses import asdict
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.json import JSON
from rich.panel import Panel
from rich.table import Table

from minileanagent import __version__
from minileanagent.agent import AgentConfig, MiniLeanAgent, RunResult
from minileanagent.environment import LocalLeanEnvironment, doctor as run_doctor
from minileanagent.models import LiteLLMModel
from minileanagent.types import ToolCall

app = typer.Typer(
    name="mini-lean",
    help="A minimal, reproducible agent baseline for Lean 4 tasks.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()


class Toolset(str, Enum):
    bash = "bash"
    lean = "lean"


class CheckerBackend(str, Enum):
    local = "local"
    kimina = "kimina"


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"mini-lean-agent {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option("--version", callback=_version_callback, is_eager=True, help="Show version."),
    ] = False,
) -> None:
    """Run Lean agents, check proofs, and inspect trajectories."""


def _slug(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-.")
    return cleaned[:80] or "task"


def _default_trajectory(project: Path, label: str = "run") -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return project / ".mini-lean-agent" / "trajectories" / f"{timestamp}-{_slug(label)}.json"


def _build_agent(
    *,
    project: Path,
    target: Path | None,
    model_name: str,
    toolset: Toolset,
    backend: CheckerBackend,
    lean_command: str | None,
    kimina_url: str | None,
    kimina_api_key: str | None,
    api_base: str | None,
    api_key: str | None,
    temperature: float,
    max_tokens: int | None,
    step_limit: int,
    cost_limit: float,
    wall_time_limit: int,
    command_timeout: int,
    max_output_chars: int,
    trajectory: Path,
) -> MiniLeanAgent:
    environment = LocalLeanEnvironment(
        project,
        target=target,
        toolset=toolset.value,
        checker_backend=backend.value,
        lean_command=lean_command,
        command_timeout_seconds=command_timeout,
        max_output_chars=max_output_chars,
        kimina_url=kimina_url,
        kimina_api_key=kimina_api_key,
    )
    model = LiteLLMModel(
        model_name=model_name,
        api_base=api_base,
        api_key=api_key,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    config = AgentConfig(
        step_limit=step_limit,
        cost_limit=cost_limit,
        wall_time_limit_seconds=wall_time_limit,
        trajectory_path=trajectory,
    )
    return MiniLeanAgent(model=model, environment=environment, config=config)


def _print_result(result: RunResult) -> None:
    table = Table(title="mini-lean result", show_header=False)
    table.add_column("field", style="bold")
    table.add_column("value")
    table.add_row("status", result.status)
    table.add_row("steps", str(result.steps))
    table.add_row("cost", f"${result.cost:.4f}")
    table.add_row("elapsed", f"{result.elapsed_seconds:.1f}s")
    table.add_row("trajectory", str(result.trajectory_path or "not saved"))
    if result.submission:
        table.add_row("submission", result.submission)
    console.print(table)


@app.command()
def solve(
    task: Annotated[str, typer.Argument(help="Natural-language Lean task to solve.")],
    project: Annotated[
        Path, typer.Option("--project", "-p", help="Lean project root.", file_okay=False)
    ] = Path("."),
    target: Annotated[
        Path | None,
        typer.Option("--target", "-t", help="Project-relative Lean file to submit."),
    ] = None,
    model: Annotated[
        str | None,
        typer.Option(
            "--model",
            "-m",
            envvar="MINI_LEAN_MODEL",
            help="LiteLLM model name, for example an OpenRouter model.",
        ),
    ] = None,
    toolset: Annotated[
        Toolset,
        typer.Option("--toolset", help="`bash` for the strict baseline or `lean` for lean_check."),
    ] = Toolset.bash,
    backend: Annotated[
        CheckerBackend,
        typer.Option("--checker", help="Lean checking backend."),
    ] = CheckerBackend.local,
    lean_command: Annotated[
        str | None,
        typer.Option(
            "--lean-command",
            help="Override checker command, for example 'lake env lean'.",
        ),
    ] = None,
    kimina_url: Annotated[
        str | None,
        typer.Option("--kimina-url", envvar="KIMINA_URL", help="Kimina server URL."),
    ] = None,
    kimina_api_key: Annotated[
        str | None,
        typer.Option(
            "--kimina-api-key",
            envvar="KIMINA_API_KEY",
            help="Kimina API key. Prefer the environment variable.",
        ),
    ] = None,
    api_base: Annotated[
        str | None,
        typer.Option("--api-base", envvar="MINI_LEAN_API_BASE", help="Custom LLM API base."),
    ] = None,
    api_key: Annotated[
        str | None,
        typer.Option(
            "--api-key",
            envvar="MINI_LEAN_API_KEY",
            help="LLM API key. Provider-specific environment variables also work.",
        ),
    ] = None,
    temperature: Annotated[float, typer.Option("--temperature")] = 0.0,
    max_tokens: Annotated[int | None, typer.Option("--max-tokens")] = None,
    step_limit: Annotated[int, typer.Option("--step-limit")] = 30,
    cost_limit: Annotated[
        float, typer.Option("--cost-limit", help="Approximate USD limit. Set 0 to disable.")
    ] = 5.0,
    wall_time_limit: Annotated[
        int, typer.Option("--wall-time-limit", help="Wall-time limit in seconds. Set 0 to disable.")
    ] = 900,
    command_timeout: Annotated[
        int, typer.Option("--command-timeout", help="Timeout for each shell/check call.")
    ] = 120,
    max_output_chars: Annotated[int, typer.Option("--max-output-chars")] = 20_000,
    trajectory: Annotated[
        Path | None,
        typer.Option("--trajectory", "-o", help="Where to save the JSON trajectory."),
    ] = None,
) -> None:
    """Run one agent on one Lean project."""
    if not model:
        console.print(
            "[red]No model configured.[/red] Pass --model or set MINI_LEAN_MODEL."
        )
        raise typer.Exit(2)

    root = project.expanduser().resolve()
    output = trajectory.expanduser().resolve() if trajectory else _default_trajectory(root, task)
    console.print(
        Panel.fit(
            "The model can execute arbitrary shell commands in the selected project. "
            "Use a disposable checkout or sandbox for untrusted models.",
            title="Security warning",
            border_style="yellow",
        )
    )
    agent = _build_agent(
        project=root,
        target=target,
        model_name=model,
        toolset=toolset,
        backend=backend,
        lean_command=lean_command,
        kimina_url=kimina_url,
        kimina_api_key=kimina_api_key,
        api_base=api_base,
        api_key=api_key,
        temperature=temperature,
        max_tokens=max_tokens,
        step_limit=step_limit,
        cost_limit=cost_limit,
        wall_time_limit=wall_time_limit,
        command_timeout=command_timeout,
        max_output_chars=max_output_chars,
        trajectory=output,
    )
    result = agent.run(task)
    _print_result(result)
    raise typer.Exit(0 if result.status == "success" else 1)


@app.command("check")
def check_file(
    path: Annotated[Path, typer.Argument(help="Project-relative Lean file to check.")],
    project: Annotated[
        Path, typer.Option("--project", "-p", help="Lean project root.", file_okay=False)
    ] = Path("."),
    backend: Annotated[CheckerBackend, typer.Option("--checker")] = CheckerBackend.local,
    lean_command: Annotated[str | None, typer.Option("--lean-command")] = None,
    kimina_url: Annotated[str | None, typer.Option("--kimina-url", envvar="KIMINA_URL")] = None,
    kimina_api_key: Annotated[
        str | None, typer.Option("--kimina-api-key", envvar="KIMINA_API_KEY")
    ] = None,
    timeout: Annotated[int, typer.Option("--timeout")] = 120,
) -> None:
    """Check a file with the same verifier used for submission."""
    environment = LocalLeanEnvironment(
        project,
        target=path,
        toolset="lean",
        checker_backend=backend.value,
        lean_command=lean_command,
        command_timeout_seconds=timeout,
        kimina_url=kimina_url,
        kimina_api_key=kimina_api_key,
    )
    observation = environment.execute(
        ToolCall(id="cli-check", name="lean_check", arguments={"path": str(path)})
    )
    console.print(JSON.from_data(observation))
    raise typer.Exit(0 if observation.get("ok") else 1)


@app.command()
def doctor(
    project: Annotated[
        Path, typer.Option("--project", "-p", help="Lean project root.", file_okay=False)
    ] = Path("."),
    lean_command: Annotated[str | None, typer.Option("--lean-command")] = None,
) -> None:
    """Check whether the local project can run mini-lean-agent."""
    result = run_doctor(project, lean_command)
    console.print(JSON.from_data(result))
    raise typer.Exit(0 if result.get("ready") else 1)


@app.command("inspect")
def inspect_trajectory(
    trajectory: Annotated[Path, typer.Argument(help="Trajectory JSON file.")],
    full: Annotated[
        bool, typer.Option("--full", help="Print the complete linear history.")
    ] = False,
) -> None:
    """Inspect a saved trajectory without a separate UI."""
    data = json.loads(trajectory.read_text(encoding="utf-8"))
    run = data.get("run") or {}
    environment = data.get("environment") or {}
    table = Table(title=str(trajectory))
    table.add_column("field", style="bold")
    table.add_column("value")
    for key in ("status", "model", "steps", "cost", "submission", "started_at", "finished_at"):
        table.add_row(key, str(run.get(key, "")))
    table.add_row("project", str(environment.get("project", "")))
    table.add_row("toolset", str(environment.get("toolset", "")))
    console.print(table)

    if full:
        for index, message in enumerate(data.get("messages") or []):
            role = message.get("role", "unknown")
            content = message.get("content", "")
            if role == "assistant" and message.get("tool_calls"):
                content = f"{content}\n\n{json.dumps(message['tool_calls'], indent=2)}"
            console.print(Panel(str(content), title=f"{index}: {role}"))


@app.command()
def batch(
    tasks: Annotated[
        Path,
        typer.Argument(
            help="JSONL file with id, task, project, and optional target fields. Runs sequentially."
        ),
    ],
    model: Annotated[
        str | None,
        typer.Option("--model", "-m", envvar="MINI_LEAN_MODEL"),
    ] = None,
    output_dir: Annotated[
        Path, typer.Option("--output-dir", "-o", help="Trajectory and result directory.")
    ] = Path("mini-lean-results"),
    toolset: Annotated[Toolset, typer.Option("--toolset")] = Toolset.bash,
    backend: Annotated[CheckerBackend, typer.Option("--checker")] = CheckerBackend.local,
    lean_command: Annotated[str | None, typer.Option("--lean-command")] = None,
    kimina_url: Annotated[str | None, typer.Option("--kimina-url", envvar="KIMINA_URL")] = None,
    kimina_api_key: Annotated[
        str | None, typer.Option("--kimina-api-key", envvar="KIMINA_API_KEY")
    ] = None,
    api_base: Annotated[
        str | None, typer.Option("--api-base", envvar="MINI_LEAN_API_BASE")
    ] = None,
    api_key: Annotated[
        str | None, typer.Option("--api-key", envvar="MINI_LEAN_API_KEY")
    ] = None,
    temperature: Annotated[float, typer.Option("--temperature")] = 0.0,
    max_tokens: Annotated[int | None, typer.Option("--max-tokens")] = None,
    step_limit: Annotated[int, typer.Option("--step-limit")] = 30,
    cost_limit: Annotated[float, typer.Option("--cost-limit")] = 5.0,
    wall_time_limit: Annotated[int, typer.Option("--wall-time-limit")] = 900,
    command_timeout: Annotated[int, typer.Option("--command-timeout")] = 120,
    max_output_chars: Annotated[int, typer.Option("--max-output-chars")] = 20_000,
) -> None:
    """Run a reproducible JSONL benchmark batch."""
    if not model:
        console.print("[red]No model configured.[/red]")
        raise typer.Exit(2)

    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    result_path = output_dir / "results.jsonl"
    result_path.unlink(missing_ok=True)
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(tasks.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise typer.BadParameter(f"Invalid JSON on line {line_number}: {exc}") from exc
        task_id = str(item.get("id") or f"line-{line_number}")
        task_text = item.get("task")
        project_value = item.get("project")
        if not isinstance(task_text, str) or not isinstance(project_value, str):
            raise typer.BadParameter(f"Line {line_number} requires string `task` and `project`")
        project_path = Path(project_value).expanduser().resolve()
        target_value = item.get("target")
        trajectory = output_dir / "trajectories" / f"{_slug(task_id)}.json"
        console.rule(f"{task_id}")
        try:
            agent = _build_agent(
                project=project_path,
                target=Path(target_value) if isinstance(target_value, str) else None,
                model_name=model,
                toolset=toolset,
                backend=backend,
                lean_command=lean_command,
                kimina_url=kimina_url,
                kimina_api_key=kimina_api_key,
                api_base=api_base,
                api_key=api_key,
                temperature=temperature,
                max_tokens=max_tokens,
                step_limit=step_limit,
                cost_limit=cost_limit,
                wall_time_limit=wall_time_limit,
                command_timeout=command_timeout,
                max_output_chars=max_output_chars,
                trajectory=trajectory,
            )
            result = agent.run(task_text)
            row = {"id": task_id, **asdict(result)}
            row["trajectory_path"] = str(result.trajectory_path) if result.trajectory_path else None
        except Exception as exc:
            row = {
                "id": task_id,
                "status": "error",
                "error_type": type(exc).__name__,
                "error": str(exc),
                "trajectory_path": str(trajectory),
            }
        rows.append(row)
        with result_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        console.print(row)

    if not rows:
        raise typer.BadParameter("The task file contains no runnable JSONL rows")

    successes = sum(row.get("status") == "success" for row in rows)
    console.print(f"Completed {len(rows)} tasks: {successes} successes. Results: {result_path}")
    raise typer.Exit(0 if successes == len(rows) else 1)


if __name__ == "__main__":
    app()
