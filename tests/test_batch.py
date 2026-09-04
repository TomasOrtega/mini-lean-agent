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


@pytest.mark.parametrize(
    ("comparator_fields", "common_comparator", "expected_comparator"),
    [
        ({}, "comparator", "comparator"),
        ({}, "/tools/common comparator", "/tools/common comparator"),
        ({"comparator": "/tools/row comparator"}, "comparator", "/tools/row comparator"),
    ],
)
def test_run_batch_writes_summary(
    tmp_path: Path, monkeypatch, comparator_fields, common_comparator: str, expected_comparator: str
) -> None:
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
                "verification_project": "verifier",
                **comparator_fields,
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
    summaries = batch.run_batch(
        tasks, output_dir=output, model_name="model", comparator=common_comparator
    )
    assert summaries[0]["verified"] is True
    assert seen["project"] == project.resolve()
    assert seen["declaration"] == "Demo.complete"
    assert seen["verification_project"] == (tmp_path / "verifier").resolve()
    assert seen["comparator"] == expected_comparator
    row = json.loads((output / "summary.jsonl").read_text(encoding="utf-8"))
    assert row["id"] == "one"


def test_empty_batch_is_rejected(tmp_path: Path) -> None:
    tasks = tmp_path / "empty.jsonl"
    tasks.write_text("\n", encoding="utf-8")
    with pytest.raises(ValueError, match="no runnable"):
        batch.run_batch(tasks, output_dir=tmp_path / "results")


@pytest.mark.parametrize(
    "verification_fields",
    [
        {},
        {"verification_project": None},
        {"verification_project": ""},
        {"verification_project": "  "},
        {"verification_project": 1},
        {"verification_project": False},
        {"verification_project": []},
        {"verification_project": {}},
    ],
)
def test_batch_requires_verification_project(
    tmp_path: Path, monkeypatch, verification_fields
) -> None:
    tasks = tmp_path / "tasks.jsonl"
    tasks.write_text(
        json.dumps(
            {
                "task": "prove",
                "project": "project",
                "target": "Main.lean",
                "declaration": "Demo.complete",
                **verification_fields,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    calls: list[object] = []

    def fake_run_task(*args, **kwargs):
        calls.append(args)
        return SimpleNamespace(to_dict=lambda: {"verified": True, "status": "success"})

    monkeypatch.setattr(batch, "run_task", fake_run_task)
    summaries = batch.run_batch(tasks, output_dir=tmp_path / "results")
    assert summaries[0]["status"] == "error"
    assert "verification_project" in summaries[0]["error"]
    assert calls == []


@pytest.mark.parametrize(
    "declaration_fields",
    [
        {},
        {"declaration": None},
        {"declaration": ""},
        {"declaration": "  "},
        {"declaration": 1},
        {"declaration": False},
        {"declaration": []},
        {"declaration": {}},
    ],
)
def test_batch_rejects_invalid_declaration_and_continues(
    tmp_path: Path, monkeypatch, declaration_fields: dict[str, object]
) -> None:
    task = {
        "task": "prove",
        "project": "project",
        "target": "Main.lean",
        "verification_project": "verifier",
    }
    tasks = tmp_path / "tasks.jsonl"
    tasks.write_text(
        "\n".join(
            json.dumps(row)
            for row in (
                {**task, "id": "invalid", **declaration_fields},
                {**task, "id": "valid", "declaration": "Demo.complete"},
            )
        )
        + "\n",
        encoding="utf-8",
    )
    declarations: list[str] = []

    def fake_run_task(task: str, **kwargs):
        declarations.append(kwargs["declaration"])
        return SimpleNamespace(to_dict=lambda: {"status": "success", "verified": True})

    monkeypatch.setattr(batch, "run_task", fake_run_task)
    output = tmp_path / "results"
    summaries = batch.run_batch(tasks, output_dir=output)
    assert summaries[0]["status"] == "error"
    assert summaries[0]["verified"] is False
    assert "declaration" in summaries[0]["error"]
    assert summaries[1]["verified"] is True
    assert declarations == ["Demo.complete"]
    rows = [json.loads(line) for line in (output / "summary.jsonl").read_text().splitlines()]
    assert rows == summaries


def test_batch_records_task_error(tmp_path: Path, monkeypatch) -> None:
    project = tmp_path / "project"
    project.mkdir()
    tasks = tmp_path / "tasks.jsonl"
    tasks.write_text(
        json.dumps(
            {
                "id": "broken",
                "task": "prove",
                "project": "project",
                "target": "Main.lean",
                "declaration": "Demo.complete",
                "verification_project": "verifier",
            }
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
