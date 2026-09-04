"""Command-line interface for the thin Lean specialization."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Annotated, Any, cast

import typer

from minileanagent import __version__
from minileanagent.batch import run_batch
from minileanagent.config import config_path
from minileanagent.runner import (
    default_trajectory_path,
    installed_upstream_version,
    run_task,
)
from minileanagent.verify import (
    project_lean_command,
    resolve_project,
    resolve_target,
    verify_target,
)

app = typer.Typer(
    name="mini-lean",
    help="Run mini-SWE-agent as a minimal Lean theorem-proving baseline.",
    no_args_is_help=True,
    invoke_without_command=True,
)

DEFAULT_PROJECT = Path(".")
DEFAULT_TARGET = Path("Main.lean")
DEFAULT_BATCH_OUTPUT = Path("mini-lean-results")


def _parse_model_config(raw: str | None) -> dict[str, Any] | None:
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise typer.BadParameter(f"Invalid JSON: {exc}", param_hint="--model-config") from exc
    if not isinstance(value, dict):
        raise typer.BadParameter("Expected a JSON object", param_hint="--model-config")
    if not all(isinstance(key, str) for key in value):
        raise typer.BadParameter(
            "Model configuration keys must be strings",
            param_hint="--model-config",
        )
    return cast(dict[str, Any], value)


@app.callback()
def root(
    version_flag: Annotated[
        bool,
        typer.Option("--version", help="Show versions and exit", is_eager=True),
    ] = False,
) -> None:
    if version_flag:
        typer.echo(f"mini-lean-agent {__version__}")
        typer.echo(f"mini-swe-agent {installed_upstream_version()}")
        raise typer.Exit()


@app.command()
def solve(
    task: Annotated[str, typer.Argument(help="Lean task or theorem-proving instruction")],
    declaration: Annotated[
        str,
        typer.Option(
            "--declaration",
            "-d",
            help="Fully-qualified theorem to compare",
        ),
    ],
    verification_project: Annotated[
        Path,
        typer.Option(
            "--verification-project",
            help="Trusted Lake project with Challenge and Solution libraries",
        ),
    ],
    project: Annotated[Path, typer.Option("--project", "-p")] = DEFAULT_PROJECT,
    target: Annotated[Path, typer.Option("--target", "-t")] = DEFAULT_TARGET,
    model: Annotated[str | None, typer.Option("--model", "-m")] = None,
    trajectory: Annotated[Path | None, typer.Option("--trajectory", "-o")] = None,
    config: Annotated[Path | None, typer.Option("--config", "-c")] = None,
    lean_command: Annotated[
        str | None,
        typer.Option("--lean-command", help="Exploratory Lean command, with optional {target}"),
    ] = None,
    model_class: Annotated[str | None, typer.Option("--model-class")] = None,
    model_config: Annotated[
        str | None,
        typer.Option("--model-config", help="JSON object merged into model config"),
    ] = None,
    step_limit: Annotated[int, typer.Option("--step-limit")] = 30,
    cost_limit: Annotated[float, typer.Option("--cost-limit")] = 3.0,
    wall_time_limit: Annotated[int, typer.Option("--wall-time-limit")] = 900,
    command_timeout: Annotated[int, typer.Option("--command-timeout")] = 120,
    verification_timeout: Annotated[int, typer.Option("--verification-timeout")] = 120,
    comparator: Annotated[
        str, typer.Option("--comparator", help="LeanFRO comparator executable")
    ] = "comparator",
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Run upstream mini-SWE-agent on one fixed Lean target."""
    try:
        project_path, _, _ = resolve_target(project, target)
        outcome = run_task(
            task,
            project=project_path,
            target=target,
            declaration=declaration,
            verification_project=verification_project,
            comparator=comparator,
            model_name=model,
            trajectory_path=trajectory or default_trajectory_path(project_path),
            step_limit=step_limit,
            cost_limit=cost_limit,
            wall_time_limit_seconds=wall_time_limit,
            command_timeout_seconds=command_timeout,
            verification_timeout_seconds=verification_timeout,
            lean_command=lean_command,
            model_class=model_class,
            model_config=_parse_model_config(model_config),
            lean_config_path=config,
        )
    except (ModuleNotFoundError, OSError, RuntimeError, ValueError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc

    if json_output:
        typer.echo(json.dumps(outcome.to_dict(), indent=2, sort_keys=True))
    else:
        typer.echo(f"status: {outcome.status}")
        typer.echo(f"verified: {str(outcome.verified).lower()}")
        typer.echo(f"upstream_exit_status: {outcome.upstream_exit_status}")
        typer.echo(f"trajectory: {outcome.trajectory_path}")
        typer.echo(f"verification: {outcome.verification.reason}")

    if not outcome.verified:
        raise typer.Exit(1)


@app.command("check")
def check_command(
    target: Annotated[Path, typer.Argument(help="Lean file to verify")],
    declaration: Annotated[
        str,
        typer.Option("--declaration", "-d", help="Fully-qualified theorem to compare"),
    ],
    verification_project: Annotated[
        Path,
        typer.Option(
            "--verification-project",
            help="Trusted Lake project with Challenge and Solution libraries",
        ),
    ],
    project: Annotated[Path, typer.Option("--project", "-p")] = DEFAULT_PROJECT,
    comparator: Annotated[
        str, typer.Option("--comparator", help="LeanFRO comparator executable")
    ] = "comparator",
    timeout: Annotated[int, typer.Option("--timeout")] = 120,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Compare a target against its trusted challenge with LeanFRO comparator."""
    try:
        result = verify_target(
            project,
            target,
            declaration=declaration,
            verification_project=verification_project,
            comparator=comparator,
            timeout_seconds=timeout,
        )
    except (OSError, ValueError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc

    if json_output:
        typer.echo(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    else:
        typer.echo(f"{result.reason}: {result.target}")
        if result.output:
            typer.echo(result.output.rstrip())
    if not result.ok:
        raise typer.Exit(1)


@app.command()
def doctor(
    project: Annotated[Path, typer.Option("--project", "-p")] = DEFAULT_PROJECT,
    target: Annotated[Path | None, typer.Option("--target", "-t")] = None,
    lean_command: Annotated[str | None, typer.Option("--lean-command")] = None,
) -> None:
    """Show package versions and the resolved Lean checker."""
    try:
        project_path = resolve_project(project)
        typer.echo(f"mini-lean-agent: {__version__}")
        typer.echo(f"mini-swe-agent: {installed_upstream_version()}")
        typer.echo(f"project: {project_path}")
        if target is not None:
            _, relative, _ = resolve_target(project_path, target)
            command = project_lean_command(project_path, relative, lean_command)
            typer.echo(f"target: {relative}")
            typer.echo(f"command: {' '.join(command)}")
            executable = shutil.which(command[0])
            if executable is None:
                raise ValueError(f"Executable is not on PATH: {command[0]}")
            typer.echo(f"executable: {executable}")
    except ValueError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc


@app.command("config-path")
def show_config_path() -> None:
    """Print the packaged Lean overlay path."""
    typer.echo(config_path())


@app.command()
def batch(
    tasks: Annotated[Path, typer.Argument(help="JSONL task file")],
    output_dir: Annotated[Path, typer.Option("--output-dir", "-o")] = DEFAULT_BATCH_OUTPUT,
    model: Annotated[str | None, typer.Option("--model", "-m")] = None,
    config: Annotated[Path | None, typer.Option("--config", "-c")] = None,
    lean_command: Annotated[str | None, typer.Option("--lean-command")] = None,
    comparator: Annotated[
        str, typer.Option("--comparator", help="LeanFRO comparator executable")
    ] = "comparator",
    model_class: Annotated[str | None, typer.Option("--model-class")] = None,
    model_config: Annotated[str | None, typer.Option("--model-config")] = None,
    step_limit: Annotated[int, typer.Option("--step-limit")] = 30,
    cost_limit: Annotated[float, typer.Option("--cost-limit")] = 3.0,
    wall_time_limit: Annotated[int, typer.Option("--wall-time-limit")] = 900,
    command_timeout: Annotated[int, typer.Option("--command-timeout")] = 120,
    verification_timeout: Annotated[int, typer.Option("--verification-timeout")] = 120,
) -> None:
    """Run isolated JSONL tasks sequentially and write upstream trajectories."""
    try:
        summaries = run_batch(
            tasks,
            output_dir=output_dir,
            model_name=model,
            step_limit=step_limit,
            cost_limit=cost_limit,
            wall_time_limit_seconds=wall_time_limit,
            command_timeout_seconds=command_timeout,
            verification_timeout_seconds=verification_timeout,
            lean_command=lean_command,
            comparator=comparator,
            model_class=model_class,
            model_config=_parse_model_config(model_config),
            lean_config_path=config,
        )
    except (ModuleNotFoundError, OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc

    failures = sum(not bool(summary.get("verified")) for summary in summaries)
    typer.echo(f"completed: {len(summaries)}")
    typer.echo(f"failures: {failures}")
    typer.echo(f"summary: {output_dir.expanduser().resolve() / 'summary.jsonl'}")
    if failures:
        raise typer.Exit(1)


@app.command("version")
def version_command() -> None:
    """Print mini-lean-agent and upstream mini-SWE-agent versions."""
    typer.echo(f"mini-lean-agent {__version__}")
    typer.echo(f"mini-swe-agent {installed_upstream_version()}")


if __name__ == "__main__":
    app()
