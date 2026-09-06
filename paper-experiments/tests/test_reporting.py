from __future__ import annotations

import csv
import hashlib
import json
from decimal import Decimal

import pytest

from paper_experiments.protocol import (
    ANCHOR_CHECKPOINTS,
    EXTENDED_CHECKPOINTS,
    GEMINI,
    LOW_CHECKPOINTS,
    MINI_CAMPAIGN_CHECKPOINTS,
    register_runs,
)
from paper_experiments.reporting import (
    ANNOTATION_FIELDS,
    IncompleteExperiment,
    _load_run,
    annotation_statistics,
    build_report,
    read_annotations,
    write_annotation_template,
    write_report,
)


def snapshot(index=0, *, phase="low", charge="1", verified=False, calls=1, seconds=60):
    return {
        "index": index,
        "phase": phase,
        "charge_usd": charge,
        "calls": calls,
        "active_seconds": seconds,
        "verification_completed_seconds": seconds + 1,
        "verification": {"verified": verified},
        "sha256": hashlib.sha256(b"example : True := by trivial\n").hexdigest(),
    }


def endpoint(snapshots, *, phase="low", charge="1", reason="agent_exit", problem="A1"):
    calls = 300 if problem in {"A5", "B6"} else 200
    seconds = 86400 if phase in {"extended", "astra", "fable"} else 7200
    thresholds = {
        "low": LOW_CHECKPOINTS,
        "extended": EXTENDED_CHECKPOINTS,
        "anchor": ANCHOR_CHECKPOINTS,
        "astra": MINI_CAMPAIGN_CHECKPOINTS,
        "fable": MINI_CAMPAIGN_CHECKPOINTS,
    }[phase]
    checkpoints = {
        str(threshold): any(
            item["verification"]["verified"]
            and Decimal(item["charge_usd"]) <= threshold
            and item["calls"] <= calls
            and item["verification_completed_seconds"] <= seconds
            for item in snapshots
        )
        for threshold in thresholds
    }
    return {
        "phase": phase,
        "stop_reason": reason,
        "snapshot_count": len(snapshots),
        "verified": checkpoints[str(thresholds[-1])],
        "checkpoints": checkpoints,
        "active_seconds": snapshots[-1]["verification_completed_seconds"] if snapshots else 0,
        "ledger": {
            "accounting_complete": True,
            "charge_usd": charge,
            "known_charge_usd": charge,
            "calls": snapshots[-1]["calls"] if snapshots else 0,
            "tokens": {"input": 1, "output": 2, "reasoning": 3, "cache_read": 4, "cache_write": 5},
            "usage_complete": True,
        },
    }


def save_run(directory, run, snapshots, low, final):
    directory.mkdir(parents=True, exist_ok=True)
    for item in snapshots:
        folder = directory / "snapshots" / f"{item['index']:06d}"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "snapshot.json").write_text(json.dumps(item))
        (folder / "candidate.lean").write_bytes(b"example : True := by trivial\n")
    state = {"run": run, "snapshots": snapshots, "low_endpoint": low, "endpoint": final}
    (directory / "controller.json").write_text(json.dumps(state))
    for record in [low, final]:
        if record is not None:
            (directory / f"{record['phase']}-endpoint.json").write_text(json.dumps(record))


@pytest.fixture
def experiment(tmp_path):
    manifest = register_runs(42)
    for run in manifest["runs"]:
        phase = "low" if run["model"] == GEMINI else "anchor"
        items = [snapshot(phase=phase)]
        end = endpoint(items, phase=phase)
        save_run(tmp_path / run["run_id"], run, items, end if phase == "low" else None, end)
    return manifest, tmp_path


def select(manifest, *, harness="mini", problem="A1", replicate=1):
    return next(
        run
        for run in manifest["runs"]
        if run["model"] == GEMINI
        and run["harness"] == harness
        and run["problem"] == problem
        and run["replicate"] == replicate
    )


def test_low_cost_denominators_include_failures_and_zero_success_is_undefined(experiment):
    manifest, runs = experiment
    report = build_report(manifest, runs)
    assert report["status"] == "complete"
    mini = report["low_budget"][0]
    assert mini["verified_at_cost"] == {"0.50": 0, "1": 0, "2": 0}
    assert mini["charge_usd"] == "12"
    assert mini["charge_per_successful_trajectory"] == {
        "usd": None,
        "denominator": 0,
        "status": "undefined (zero successes)",
    }
    assert report["realized_charge_usd"] == {
        "low": "24",
        "additional_continuation": "0",
        "anchor": "12",
        "total": "36",
    }


def test_continuation_cannot_retroactively_improve_frozen_low_budget(experiment):
    manifest, runs = experiment
    run = select(manifest, problem="A5")
    prefix = [snapshot(seconds=7200)]
    low = endpoint(prefix, reason="elapsed_time")
    items = [
        *prefix,
        snapshot(1, phase="extended", charge="1.50", verified=True, seconds=7300, calls=2),
    ]
    final = endpoint(items, phase="extended", charge="1.50", reason="verified")
    save_run(runs / run["run_id"], run, items, low, final)
    report = build_report(manifest, runs)
    assert report["low_budget"][0]["verified_at_cost"]["2"] == 0
    hard = report["extended"][0]
    assert hard["low_verified"] is False
    assert hard["verified_at_cost"] == {"5": True, "10": True, "25": True, "50": True}
    assert hard["new_success_after_low"] is True
    assert hard["qualifying_artifact_predates_low_freeze"] is False
    assert Decimal(hard["additional_continuation_charge_usd"]) == Decimal("0.50")
    assert Decimal(report["realized_charge_usd"]["total"]) == Decimal("36.50")


def test_cap_crossing_success_is_charged_but_not_credited_below_cost(experiment):
    manifest, runs = experiment
    run = select(manifest, problem="B6")
    items = [snapshot(charge="2.10", verified=True)]
    low = endpoint(items, charge="2.10", reason="cost")
    final = endpoint(items, phase="extended", charge="2.10", reason="verified")
    save_run(runs / run["run_id"], run, items, low, final)
    report = build_report(manifest, runs)
    assert report["low_budget"][0]["verified_at_cost"]["2"] == 0
    hard = next(row for row in report["extended"] if row["run_id"] == run["run_id"])
    assert hard["verified"] is True
    assert hard["qualifying_artifact_predates_low_freeze"] is True
    assert Decimal(hard["additional_continuation_charge_usd"]) == 0
    assert report["overshoot"]["low"]["affected_phase_endpoints"] == 1
    assert Decimal(report["overshoot"]["low"]["charge_above_nominal_cap_usd"]) == Decimal("0.10")


@pytest.mark.parametrize("calls,seconds", [(201, 60), (1, 7200)])
def test_call_and_verification_time_limits_are_enforced(experiment, calls, seconds):
    manifest, runs = experiment
    run = select(manifest)
    items = [snapshot(verified=True, calls=calls, seconds=seconds)]
    low = endpoint(items)
    save_run(runs / run["run_id"], run, items, low, low)
    report = build_report(manifest, runs)
    assert report["low_budget"][0]["verified_at_cost"]["2"] == 0


def test_one_attempt_reports_single_run_success_and_cost(experiment):
    manifest, runs = experiment
    run = select(manifest)
    items = [snapshot(charge="0.50", verified=True)]
    low = endpoint(items, charge="0.50", reason="verified")
    save_run(runs / run["run_id"], run, items, low, low)
    row = build_report(manifest, runs)["low_budget"][0]
    assert row["verified_at_cost"]["0.50"] == 1
    assert row["registered_trajectories"] == 12
    assert row["single_run_rate_percent"] == pytest.approx(100 / 12)
    assert row["charge_per_successful_trajectory"]["usd"] == "11.50"
    assert "solved_at_3" not in row
    assert "charge_per_distinct_problem_solved_at_3" not in row


def test_missing_run_requires_explicit_incomplete_report(experiment):
    manifest, runs = experiment
    run = select(manifest)
    (runs / run["run_id"] / "controller.json").unlink()
    with pytest.raises(IncompleteExperiment, match=run["run_id"]):
        build_report(manifest, runs)
    report = build_report(manifest, runs, allow_incomplete=True)
    assert report["status"] == "incomplete"
    assert "paired_effect" not in report
    assert report["low_budget"][0]["verified_at_cost"]["2"] is None
    assert report["realized_charge_usd"]["total"] is None


def test_missing_predeclared_continuation_cannot_be_silently_skipped(experiment):
    manifest, runs = experiment
    run = select(manifest, problem="A5")
    items = [snapshot(charge="2")]
    low = endpoint(items, charge="2", reason="cost")
    save_run(runs / run["run_id"], run, items, low, low)
    with pytest.raises(IncompleteExperiment, match="missing predeclared continuation"):
        build_report(manifest, runs)
    report = build_report(manifest, runs, allow_incomplete=True)
    assert report["extended"][0]["verified"] is None
    assert report["low_budget"][0]["verified_at_cost"]["2"] == 0


def test_infrastructure_failure_is_retained_as_unsuccessful(experiment):
    manifest, runs = experiment
    run = select(manifest)
    items = [snapshot()]
    low = endpoint(items, reason="infrastructure_failure")
    save_run(runs / run["run_id"], run, items, low, low)
    report = build_report(manifest, runs)
    assert report["low_budget"][0]["registered_trajectories"] == 12
    assert report["low_budget"][0]["charge_usd"] == "12"
    assert report["infrastructure_failure_sensitivity"]["affected_endpoints"] == [
        {"run_id": run["run_id"], "phase": "low", "charge_usd": "1"}
    ]


def test_modified_candidate_is_rejected(experiment):
    manifest, runs = experiment
    run = select(manifest)
    (runs / run["run_id"] / "snapshots" / "000000" / "candidate.lean").write_text(
        "example : False := by sorry\n"
    )
    with pytest.raises(ValueError, match="Changed immutable candidate"):
        build_report(manifest, runs)


def test_annotation_template_retains_ids_and_never_overwrites_work(tmp_path):
    manifest = register_runs(4)
    output = tmp_path / "annotations.csv"
    write_annotation_template(manifest, output)
    assert len(list(csv.DictReader(output.open()))) == 36
    assert read_annotations(output, manifest) == {}
    assert "Blank cells mean" in output.with_suffix(".rubric.md").read_text()
    with pytest.raises(FileExistsError):
        write_annotation_template(manifest, output)


def test_annotations_report_actual_denominators_and_recovery(tmp_path):
    manifest = register_runs(4)
    run = select(manifest)
    path = tmp_path / "annotations.csv"
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=ANNOTATION_FIELDS)
        writer.writeheader()
        writer.writerow(
            {
                "run_id": run["run_id"],
                "mathlib_search": "true",
                "scratch_files": "false",
                "lean_invocations": "3",
                "first_lean_check_failed": "true",
                "failure_category": "none",
                "evidence": "trace events 2, 4, 7",
            }
        )
    annotations = read_annotations(path, manifest)
    result = annotation_statistics(manifest, {run["run_id"]: True}, annotations)
    assert result["status"] == "incomplete annotations"
    assert result["search_among_annotated_successes"] == {
        "numerator": 1,
        "denominator": 1,
        "percent": 100,
    }
    assert result["recovery_among_annotated_first_check_failures"]["denominator"] == 1
    assert result["median_lean_invocations_among_annotated_successes"] == 3


def test_report_exports_all_paper_panels_and_undefined_costs(experiment, tmp_path):
    manifest, runs = experiment
    report = build_report(manifest, runs)
    output = tmp_path / "report"
    write_report(report, output)
    for name in (
        "low-budget",
        "per-problem",
        "extended",
        "hard-checkpoints",
        "historical",
        "charges",
    ):
        assert (output / f"{name}.csv").exists()
        assert r"\color{blue}" in (output / f"{name}.tex").read_text()
    assert "undefined (zero successes)" in (output / "summary.txt").read_text()
    exported = json.loads((output / "report.json").read_text())
    assert exported["status"] == "complete"
    assert "paired_effect" not in exported
    assert not any(
        term in (output / "summary.txt").read_text() for term in ("bootstrap", "95%", "resamples")
    )


def test_a1_report_uses_only_selected_denominators(tmp_path):
    manifest = register_runs(2025, problems=["A1"])
    for run in manifest["runs"]:
        phase = "low" if run["model"] == GEMINI else "anchor"
        items = [snapshot(phase=phase, verified=run["harness"] == "axprover")]
        end = endpoint(items, phase=phase)
        save_run(tmp_path / run["run_id"], run, items, end if phase == "low" else None, end)
    report = build_report(manifest, tmp_path)
    assert report["status"] == "complete"
    assert report["scope"] == manifest["scope"]
    assert [row["registered_trajectories"] for row in report["low_budget"]] == [1, 1]
    assert report["historical_anchor"]["registered_trajectories"] == 1
    assert report["historical_anchor"]["status"] == "complete"
    assert [row["problem"] for row in report["per_problem"]] == ["A1"]
    assert report["extended"] == []
    assert "paired_effect" not in report
    assert report["realized_charge_usd"] == {
        "low": "2",
        "additional_continuation": "0",
        "anchor": "1",
        "total": "3",
    }
    output = tmp_path / "report"
    write_report(report, output)
    summary = (output / "summary.txt").read_text()
    assert "Selected-problem pilot: A1 (1 of 12 benchmark problems)" in summary
    assert "Selected-problem pilot: A1" in (output / "low-budget.tex").read_text()
    history = list(csv.reader((output / "historical.csv").open()))
    assert len(history) == 3
    assert all(row[1] == "0/1" for row in history[1:])
    assert "12/12" not in (output / "historical.csv").read_text()


def test_selected_report_still_requires_all_three_registered_endpoints(tmp_path):
    manifest = register_runs(1, problems=["A1"])
    with pytest.raises(IncompleteExperiment, match="A1"):
        build_report(manifest, tmp_path)
    report = build_report(manifest, tmp_path, allow_incomplete=True)
    assert report["status"] == "incomplete"
    assert len(report["missing_or_incomplete"]) == 3
    assert report["historical_anchor"]["registered_trajectories"] == 1


def test_continuation_charge_cannot_drop_below_frozen_prefix(experiment):
    manifest, runs = experiment
    run = select(manifest, problem="A5")
    items = [snapshot()]
    low = endpoint(items, charge="2", reason="cost")
    final = endpoint(items, phase="extended", charge="1.50", reason="agent_exit")
    save_run(runs / run["run_id"], run, items, low, final)
    with pytest.raises(ValueError, match="cumulative continuation ledger"):
        build_report(manifest, runs)


def test_incomplete_charge_remains_pending_without_replacing_with_zero(experiment):
    manifest, runs = experiment
    run = select(manifest)
    items = [snapshot()]
    low = endpoint(items, reason="infrastructure_failure")
    low["ledger"]["accounting_complete"] = False
    low["ledger"]["charge_usd"] = None
    save_run(runs / run["run_id"], run, items, low, low)
    with pytest.raises(IncompleteExperiment, match="incomplete low charge"):
        build_report(manifest, runs)
    report = build_report(manifest, runs, allow_incomplete=True)
    assert report["low_budget"][0]["charge_usd"] is None
    assert report["realized_charge_usd"]["total"] is None


def test_terminal_designated_run_does_not_require_impossible_continuation(experiment):
    manifest, runs = experiment
    run = select(manifest, problem="A5")
    items = [snapshot(charge="2")]
    low = endpoint(items, charge="2", reason="cost")
    low["resumable"] = False
    save_run(runs / run["run_id"], run, items, low, low)
    report = build_report(manifest, runs)
    assert report["status"] == "complete"
    hard = report["extended"][0]
    assert hard["continued"] is False
    assert hard["verified"] is False
    assert hard["stop_reason"] == "cost"
    assert Decimal(hard["additional_continuation_charge_usd"]) == 0


@pytest.mark.parametrize("problem,cap", [("A1", 200), ("A5", 300), ("B6", 300)])
@pytest.mark.parametrize("model,phase", [(GEMINI, "low"), ("claude-opus-4-5", "anchor")])
@pytest.mark.parametrize("extra_call", [0, 1])
def test_report_uses_problem_specific_call_caps(experiment, problem, cap, model, phase, extra_call):
    manifest, runs = experiment
    run = next(
        r
        for r in manifest["runs"]
        if r["model"] == model and r["harness"] == "mini" and r["problem"] == problem
    )
    items = [snapshot(phase=phase, verified=True, calls=cap + extra_call)]
    end = endpoint(items, phase=phase, problem=problem)
    save_run(runs / run["run_id"], run, items, end if phase == "low" else None, end)
    report = build_report(manifest, runs)
    row = report["low_budget"][0] if phase == "low" else report["historical_anchor"]
    assert row["verified_at_cost"]["2"] == (1 if extra_call == 0 else 0)


def test_exports_use_twelve_single_attempt_denominators(experiment, tmp_path):
    manifest, runs = experiment
    output = tmp_path / "single-attempt-report"
    write_report(build_report(manifest, runs), output)
    with (output / "low-budget.csv").open() as stream:
        reader = csv.DictReader(stream)
        assert reader.fieldnames == ["Harness", "$0.50", "$1", "$2", "Charge"]
        assert all(row["$2"] == "0/12" for row in reader)
    with (output / "per-problem.csv").open() as stream:
        assert all(
            row["mini success"] == row["Ax success"] == "0/1" for row in csv.DictReader(stream)
        )
    assert "solved_at_3" not in (output / "report.json").read_text()


@pytest.mark.parametrize("problem", ["A5", "B6"])
@pytest.mark.parametrize("calls,accepted", [(300, True), (301, False)])
def test_extended_report_uses_cumulative_three_hundred_call_cap(
    experiment, problem, calls, accepted
):
    manifest, runs = experiment
    run = select(manifest, problem=problem)
    prefix = [snapshot(charge="2", calls=100)]
    low = endpoint(prefix, charge="2", reason="cost", problem=problem)
    items = [
        *prefix,
        snapshot(1, phase="extended", charge="3", verified=True, calls=calls, seconds=600),
    ]
    final = endpoint(items, phase="extended", charge="3", problem=problem)
    save_run(runs / run["run_id"], run, items, low, final)
    report = build_report(manifest, runs)
    hard = next(row for row in report["extended"] if row["run_id"] == run["run_id"])
    assert hard["verified"] is accepted
    assert hard["calls"] == calls


@pytest.mark.parametrize("campaign", ["astra", "fable"])
def test_modern_campaign_loader_preserves_strict_checkpoints_and_rejects_tampering(
    tmp_path, campaign
):
    run = register_runs(2025, campaign=campaign)["runs"][0]
    items = [snapshot(phase=campaign, charge="3", verified=True)]
    end = endpoint(items, phase=campaign, charge="3", reason="verified")
    directory = tmp_path / run["run_id"]
    save_run(directory, run, items, None, end)
    state = _load_run(run, directory)
    assert state["low_endpoint"] is None
    assert state["endpoint"]["checkpoints"] == {
        "2": False,
        "5": True,
        "10": True,
        "20": True,
        "50": True,
    }
    end["checkpoints"]["2"] = True
    save_run(directory, run, items, None, end)
    with pytest.raises(ValueError, match="Frozen checkpoints disagree"):
        _load_run(run, directory)
