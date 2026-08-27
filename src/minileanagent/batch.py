"""A small sequential JSONL adapter around :func:`run_task`."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, cast

from minileanagent.runner import run_task


def read_tasks(path: Path) -> Iterable[dict[str, Any]]:
    """Yield JSON objects from a JSONL task file."""
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"Line {line_number} is not a JSON object")
            if not all(isinstance(key, str) for key in value):
                raise ValueError(f"Line {line_number} has a non-string object key")
            yield cast(dict[str, Any], value)


def safe_id(value: object, index: int) -> str:
    """Convert a task identifier into a safe file stem."""
    raw = str(value) if value is not None else f"task-{index}"
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", raw).strip("-")
    return cleaned or f"task-{index}"


def _task_project(tasks_path: Path, value: object) -> Path:
    project = Path(str(value)).expanduser()
    if not project.is_absolute():
        project = tasks_path.parent / project
    return project.resolve()


def run_batch(
    tasks_path: Path,
    *,
    output_dir: Path,
    model_name: str | None = None,
    step_limit: int = 30,
    cost_limit: float = 3.0,
    wall_time_limit_seconds: int = 900,
    command_timeout_seconds: int = 120,
    verification_timeout_seconds: int = 120,
    lean_command: str | None = None,
    model_class: str | None = None,
    model_config: Mapping[str, Any] | None = None,
    lean_config_path: Path | None = None,
) -> list[dict[str, Any]]:
    """Run JSONL tasks sequentially and write one upstream trajectory per task."""
    tasks_path = tasks_path.expanduser().resolve()
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    summary_path = output_dir / "summary.jsonl"
    summary_path.write_text("", encoding="utf-8")
    summaries: list[dict[str, Any]] = []

    for index, task in enumerate(read_tasks(tasks_path), start=1):
        task_id = safe_id(task.get("id"), index)
        trajectory = output_dir / f"{task_id}.traj.json"
        summary: dict[str, Any] = {"id": task_id, "trajectory_path": str(trajectory)}
        try:
            outcome = run_task(
                str(task["task"]),
                project=_task_project(tasks_path, task["project"]),
                target=Path(str(task["target"])),
                declaration=str(task["declaration"]) if task.get("declaration") else None,
                model_name=str(task["model"]) if task.get("model") else model_name,
                trajectory_path=trajectory,
                step_limit=int(task.get("step_limit", step_limit)),
                cost_limit=float(task.get("cost_limit", cost_limit)),
                wall_time_limit_seconds=int(
                    task.get("wall_time_limit_seconds", wall_time_limit_seconds)
                ),
                command_timeout_seconds=int(
                    task.get("command_timeout_seconds", command_timeout_seconds)
                ),
                verification_timeout_seconds=int(
                    task.get("verification_timeout_seconds", verification_timeout_seconds)
                ),
                lean_command=(
                    str(task["lean_command"])
                    if task.get("lean_command")
                    else lean_command
                ),
                model_class=model_class,
                model_config=model_config,
                lean_config_path=lean_config_path,
            )
            summary.update(outcome.to_dict())
            summary["error"] = ""
        except Exception as exc:
            summary.update(
                {
                    "status": "error",
                    "verified": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
        summaries.append(summary)
        with summary_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(summary, sort_keys=True) + "\n")

    if not summaries:
        raise ValueError(f"Task file contains no runnable JSON objects: {tasks_path}")
    return summaries
