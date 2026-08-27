from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from minileanagent import batch


def test_read_tasks_rejects_non_object(tmp_path: Path) -> None:
    path = tmp_path / "tasks.jsonl"
    path.write_text("[]\n", encoding="utf-8")
    with pytest.raises(ValueError, match="not a JSON object"):
        list(batch.read_tasks(path))


def test_safe_id() -> None:
    assert batch.safe_id("a task/one", 1) == "a-task-one"
    assert batch.safe_id(None, 2) == "task-2"


def test_run_batch_writes_summary(tmp_path: Path, monkeypatch) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "Main.lean").write_text("#check Nat\n", encoding="utf-8")
    tasks = tmp_path / "tasks.jsonl"
    tasks.write_text(
        json.dumps(
            {
                "id": "one",
                "task": "prove",
                "project": "project",
                "target": "Main.lean",
                "declaration": "Demo.complete",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    seen: dict[str, object] = {}

    def fake_run_task(task: str, **kwargs):
        seen["task"] = task
        seen.update(kwargs)
        return SimpleNamespace(
            to_dict=lambda: {
                "status": "success",
                "verified": True,
                "trajectory_path": str(kwargs["trajectory_path"]),
            }
        )

    monkeypatch.setattr(batch, "run_task", fake_run_task)
    output = tmp_path / "results"
    summaries = batch.run_batch(tasks, output_dir=output, model_name="model")
    assert summaries[0]["verified"] is True
    assert seen["project"] == project.resolve()
    assert seen["declaration"] == "Demo.complete"
    row = json.loads((output / "summary.jsonl").read_text(encoding="utf-8"))
    assert row["id"] == "one"


def test_empty_batch_is_rejected(tmp_path: Path) -> None:
    tasks = tmp_path / "empty.jsonl"
    tasks.write_text("\n", encoding="utf-8")
    with pytest.raises(ValueError, match="no runnable"):
        batch.run_batch(tasks, output_dir=tmp_path / "results")


def test_batch_records_task_error(tmp_path: Path, monkeypatch) -> None:
    project = tmp_path / "project"
    project.mkdir()
    tasks = tmp_path / "tasks.jsonl"
    tasks.write_text(
        json.dumps(
            {"id": "broken", "task": "prove", "project": "project", "target": "Main.lean"}
        )
        + "\n",
        encoding="utf-8",
    )

    def fail(*args, **kwargs):
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(batch, "run_task", fail)
    summaries = batch.run_batch(tasks, output_dir=tmp_path / "results")
    assert summaries[0]["status"] == "error"
    assert "synthetic failure" in summaries[0]["error"]
