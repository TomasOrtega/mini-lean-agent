"""Cost-qualified tables and accounting from immutable run records."""

from __future__ import annotations

import csv
import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from statistics import median
from typing import Any

from .protocol import (
    ANCHOR_CHECKPOINTS,
    EXTENDED_CHECKPOINTS,
    GEMINI,
    HARNESSES,
    LOW_CHECKPOINTS,
    MINI_CAMPAIGN_CHECKPOINTS,
    OPUS,
    Limits,
    validate_registration,
)

ANNOTATION_FIELDS = (
    "run_id",
    "mathlib_search",
    "scratch_files",
    "lean_invocations",
    "first_lean_check_failed",
    "failure_category",
    "evidence",
)
FAILURE_CATEGORIES = (
    "none",
    "proof",
    "library",
    "type",
    "context",
    "limit",
    "infrastructure",
    "unknown",
)
ANNOTATION_RUBRIC = """# Manual trace annotation rubric

Annotate the frozen low-budget portion of each Gemini trajectory; never include
continuation events. Opus rows cover its full anchor trajectory. Blank cells mean
not yet annotated, and must not be interpreted as false or zero.

- mathlib_search: true only for an observed shell command querying the permitted
  Mathlib source or declarations; false when the full trace contains no such event.
- scratch_files: true for an observed Lean scratch file created outside the declared
  target; false when the full trace contains no such event.
- lean_invocations: count executed compiler/build/checker commands, including failed
  commands; exclude commands merely mentioned in model prose and evaluator calls.
- first_lean_check_failed: true if the first observed agent-side Lean invocation
  reports an error; false if it succeeds. Leave blank if no check occurred.
- failure_category: choose the terminal observed obstacle: proof (an explicit
  unresolved proof goal), library (an unknown/missing library declaration), type
  (a reported elaboration/type mismatch), context (provider context exhaustion),
  limit (a cost/call/time bound with no more specific observed obstacle),
  infrastructure (provider/tool/evaluator infrastructure error), none (accepted),
  or unknown. Do not infer a model's ability or causal diagnosis from an error.
- evidence: cite transcript event IDs or file/line references supporting the labels.

Review every required field before treating a row as annotated. Recovery after a
failed first Lean check is derived from independently accepted low-budget artifacts,
not from a reviewer or a model's self-report. Report annotation coverage and the
actual denominators, alongside all percentages. These labels are manual evidence;
command-name heuristics do not replace the rubric.
"""


class IncompleteExperiment(ValueError):
    """Registered trajectories or trusted accounting required for a report are missing."""


def _ratio(charge: Decimal, successes: int) -> dict[str, Any]:
    return {
        "usd": str(charge / successes) if successes else None,
        "denominator": successes,
        "status": "defined" if successes else "undefined (zero successes)",
    }


def write_annotation_template(manifest: dict[str, Any], output: Path) -> None:
    """Seed immutable registered IDs while preserving any existing human annotations."""
    validate_registration(manifest)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=ANNOTATION_FIELDS)
        writer.writeheader()
        writer.writerows({"run_id": run["run_id"]} for run in manifest["runs"])
    output.with_suffix(".rubric.md").write_text(ANNOTATION_RUBRIC)


def read_annotations(path: Path, manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    allowed = {run["run_id"] for run in manifest["runs"]}
    result = {}
    with path.open(newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != list(ANNOTATION_FIELDS):
            raise ValueError(f"Expected annotation CSV columns: {ANNOTATION_FIELDS}")
        seen = set()
        for row in reader:
            run_id = row["run_id"]
            if run_id not in allowed or run_id in seen:
                raise ValueError(f"Unknown or duplicate annotation run: {run_id}")
            seen.add(run_id)
            values: dict[str, Any] = {"run_id": run_id}
            for field in ("mathlib_search", "scratch_files", "first_lean_check_failed"):
                raw = row[field].strip().lower()
                if raw not in {"", "true", "false"}:
                    raise ValueError(f"{run_id}: {field} must be true, false, or blank")
                values[field] = {"true": True, "false": False, "": None}[raw]
            raw = row["lean_invocations"].strip()
            if raw and (not raw.isdecimal() or int(raw) < 0):
                raise ValueError(f"{run_id}: Lean invocations must be a non-negative integer")
            values["lean_invocations"] = int(raw) if raw else None
            category = row["failure_category"].strip()
            if category and category not in FAILURE_CATEGORIES:
                raise ValueError(f"{run_id}: unknown failure category {category!r}")
            values["failure_category"] = category or None
            values["evidence"] = row["evidence"].strip()
            complete = all(
                values[field] is not None
                for field in (
                    "mathlib_search",
                    "scratch_files",
                    "lean_invocations",
                    "failure_category",
                )
            ) and bool(values["evidence"])
            if (
                complete
                and values["lean_invocations"]
                and values["first_lean_check_failed"] is None
            ):
                complete = False
            if complete:
                result[run_id] = values
    return result


def annotation_statistics(
    manifest: dict[str, Any], low_success: dict[str, bool], annotations: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    mini_ids = {
        run["run_id"]
        for run in manifest["runs"]
        if run["model"] == GEMINI and run["harness"] == "mini"
    }
    available = mini_ids & annotations.keys() & low_success.keys()
    successful = [annotations[run_id] for run_id in sorted(available) if low_success[run_id]]
    first_failed = [
        run_id for run_id in available if annotations[run_id]["first_lean_check_failed"]
    ]

    def proportion(numerator: int, denominator: int) -> dict[str, Any]:
        return {
            "numerator": numerator,
            "denominator": denominator,
            "percent": 100 * numerator / denominator if denominator else None,
        }

    return {
        "status": "complete" if len(available) == len(mini_ids) else "incomplete annotations",
        "annotated_low_mini_runs": len(available),
        "registered_low_mini_runs": len(mini_ids),
        "annotated_successful_low_mini_runs": len(successful),
        "search_among_annotated_successes": proportion(
            sum(a["mathlib_search"] for a in successful), len(successful)
        ),
        "scratch_among_annotated_successes": proportion(
            sum(a["scratch_files"] for a in successful), len(successful)
        ),
        "median_lean_invocations_among_annotated_successes": median(
            a["lean_invocations"] for a in successful
        )
        if successful
        else None,
        "recovery_among_annotated_first_check_failures": proportion(
            sum(low_success[run_id] for run_id in first_failed), len(first_failed)
        ),
        "failure_categories": {
            category: sum(
                annotations[run_id]["failure_category"] == category for run_id in available
            )
            for category in FAILURE_CATEGORIES
        },
        "rubric": "manual annotation; frozen Gemini low-budget prefixes; no heuristic inference",
    }


def _eligible(
    snapshot: dict[str, Any], threshold: Decimal, phase: str, run: dict[str, Any]
) -> bool:
    limits = Limits.for_run(run, phase)
    charge = snapshot["charge_usd"]
    return bool(
        charge is not None
        and Decimal(charge) <= threshold
        and snapshot["calls"] <= limits.calls
        and snapshot["active_seconds"] <= limits.active_seconds
        and snapshot["verification_completed_seconds"] <= limits.active_seconds
        and snapshot["verification"].get("verified") is True
    )


def _load_run(run: dict[str, Any], directory: Path) -> dict[str, Any] | None:
    import hashlib

    path = directory / "controller.json"
    if not path.exists():
        return None
    state = json.loads(path.read_text())
    if state["run"] != run:
        raise ValueError(f"Changed registration in {path}")
    previous_calls, previous_seconds, previous_charge = 0, 0, Decimal(0)
    for index, snapshot in enumerate(state["snapshots"]):
        folder = directory / "snapshots" / f"{index:06d}"
        if (
            snapshot["index"] != index
            or json.loads((folder / "snapshot.json").read_text()) != snapshot
        ):
            raise ValueError(f"Changed snapshot metadata in {folder}")
        if (
            hashlib.sha256((folder / "candidate.lean").read_bytes()).hexdigest()
            != snapshot["sha256"]
        ):
            raise ValueError(f"Changed immutable candidate in {folder}")
        if snapshot["calls"] < previous_calls or snapshot["active_seconds"] < previous_seconds:
            raise ValueError(f"Non-monotone artifact provenance in {folder}")
        if snapshot["verification_completed_seconds"] < snapshot["active_seconds"]:
            raise ValueError(f"Verification predates artifact availability in {folder}")
        charge = snapshot["charge_usd"]
        if charge is not None:
            if not Decimal(charge).is_finite() or Decimal(charge) < previous_charge:
                raise ValueError(f"Invalid cumulative artifact charge in {folder}")
            previous_charge = Decimal(charge)
        previous_calls, previous_seconds = snapshot["calls"], snapshot["active_seconds"]
    for name in ("low_endpoint", "endpoint"):
        endpoint = state.get(name)
        if endpoint is None:
            continue
        phase = endpoint["phase"]
        frozen = directory / f"{phase}-endpoint.json"
        if not frozen.exists() or json.loads(frozen.read_text()) != endpoint:
            raise ValueError(f"Changed or missing frozen {phase} endpoint in {directory}")
        count = endpoint["snapshot_count"]
        if not 0 <= count <= len(state["snapshots"]):
            raise ValueError(f"Invalid frozen snapshot prefix in {directory}")
        snapshots = state["snapshots"][:count]
        allowed_phases = {"low", "extended"} if phase == "extended" else {phase}
        if any(snapshot["phase"] not in allowed_phases for snapshot in snapshots):
            raise ValueError(f"A later phase contaminated the frozen {phase} prefix")
        thresholds = {
            "low": LOW_CHECKPOINTS,
            "extended": EXTENDED_CHECKPOINTS,
            "anchor": ANCHOR_CHECKPOINTS,
            "astra": MINI_CAMPAIGN_CHECKPOINTS,
            "fable": MINI_CAMPAIGN_CHECKPOINTS,
        }[phase]
        checkpoints = {
            str(threshold): any(
                _eligible(snapshot, threshold, phase, run) for snapshot in snapshots
            )
            for threshold in thresholds
        }
        if (
            endpoint["checkpoints"] != checkpoints
            or endpoint["verified"] != checkpoints[str(thresholds[-1])]
        ):
            raise ValueError(
                f"Frozen checkpoints disagree with cost-qualified artifacts in {directory}"
            )
        ledger = endpoint["ledger"]
        if ledger["accounting_complete"]:
            charge = Decimal(ledger["charge_usd"])
            if not charge.is_finite() or charge < 0:
                raise ValueError(f"Invalid endpoint charge in {directory}")
            for snapshot in snapshots:
                if snapshot["calls"] > ledger["calls"] or (
                    snapshot["charge_usd"] is not None and Decimal(snapshot["charge_usd"]) > charge
                ):
                    raise ValueError(
                        f"Endpoint ledger predates an included snapshot in {directory}"
                    )
    low, final = state.get("low_endpoint"), state.get("endpoint")
    if final is not None and final["phase"] == "extended":
        if not run["extendable"] or low is None:
            raise ValueError(f"Unregistered continuation in {directory}")
        prior, current = low["ledger"], final["ledger"]
        if (
            final["snapshot_count"] < low["snapshot_count"]
            or final["active_seconds"] < low["active_seconds"]
            or current["calls"] < prior["calls"]
            or (
                prior["accounting_complete"]
                and current["accounting_complete"]
                and Decimal(current["charge_usd"]) < Decimal(prior["charge_usd"])
            )
        ):
            raise ValueError(f"Invalid cumulative continuation ledger in {directory}")
    return state


def _endpoint_row(
    run: dict[str, Any], endpoint: dict[str, Any], started_at: str | None = None
) -> dict[str, Any]:
    ledger = endpoint["ledger"]
    return {
        **{name: run[name] for name in ("run_id", "model", "harness", "problem", "replicate")},
        "phase": endpoint["phase"],
        "verified": endpoint["verified"],
        "stop_reason": endpoint["stop_reason"],
        "charge_usd": ledger["charge_usd"],
        "known_charge_usd": ledger["known_charge_usd"],
        "accounting_complete": ledger["accounting_complete"],
        "calls": ledger["calls"],
        "active_seconds": endpoint["active_seconds"],
        "calendar_seconds": (
            datetime.fromisoformat(endpoint["frozen_at"]) - datetime.fromisoformat(started_at)
        ).total_seconds()
        if started_at is not None
        else None,
        **{f"{category}_tokens": value for category, value in ledger["tokens"].items()},
        "usage_complete": ledger.get("usage_complete", False),
    }


def build_report(
    manifest: dict[str, Any],
    runs_dir: Path,
    *,
    allow_incomplete: bool = False,
    annotations_path: Path | None = None,
) -> dict[str, Any]:
    """Require all registered endpoints unless explicitly generating a pending report."""
    validate_registration(manifest)
    selected = manifest["scope"]["problems"]
    problems: list[str] = []
    states = {}
    endpoints: dict[str, dict[str, Any]] = {}
    final_endpoints: dict[str, dict[str, Any]] = {}
    per_run = []
    low_success: dict[str, bool] = {}
    for run in manifest["runs"]:
        run_id = run["run_id"]
        state = _load_run(run, Path(runs_dir) / run_id)
        states[run_id] = state
        first_phase = "low" if run["model"] == GEMINI else "anchor"
        endpoint = (
            None
            if state is None
            else state.get("low_endpoint")
            if first_phase == "low"
            else state.get("endpoint")
        )
        if endpoint is None:
            problems.append(f"{run_id}: missing frozen {first_phase} endpoint")
            continue
        if endpoint["phase"] != first_phase:
            raise ValueError(f"Wrong initial endpoint phase for {run_id}")
        if not endpoint["ledger"]["accounting_complete"]:
            problems.append(f"{run_id}: incomplete {first_phase} charge accounting")
        endpoints[run_id] = endpoint
        per_run.append(_endpoint_row(run, endpoint, state.get("started_at")))
        if first_phase == "low":
            low_success[run_id] = endpoint["checkpoints"]["2"]
        final_endpoint = endpoint
        continuation_needed = (
            run["extendable"]
            and not endpoint["verified"]
            and endpoint.get("resumable", True)
            and endpoint["stop_reason"] in {"cost", "call_count", "elapsed_time"}
        )
        if continuation_needed:
            final_endpoint = state.get("endpoint")
            if final_endpoint is None or final_endpoint["phase"] != "extended":
                problems.append(f"{run_id}: missing predeclared continuation endpoint")
                continue
            if not final_endpoint["ledger"]["accounting_complete"]:
                problems.append(f"{run_id}: incomplete continuation charge accounting")
            per_run.append(_endpoint_row(run, final_endpoint, state.get("started_at")))
        final_endpoints[run_id] = final_endpoint
    if problems and not allow_incomplete:
        raise IncompleteExperiment("Cannot report a complete experiment:\n" + "\n".join(problems))

    low_rows, problem_rows = [], []
    for harness in HARNESSES:
        runs = [
            run for run in manifest["runs"] if run["model"] == GEMINI and run["harness"] == harness
        ]
        available = [run for run in runs if run["run_id"] in endpoints]
        complete = len(available) == len(runs) and all(
            endpoints[run["run_id"]]["ledger"]["accounting_complete"] for run in available
        )
        counts = {
            str(threshold): sum(
                endpoints[run["run_id"]]["checkpoints"][str(threshold)] for run in available
            )
            if complete
            else None
            for threshold in LOW_CHECKPOINTS
        }
        charge = (
            sum(
                (Decimal(endpoints[run["run_id"]]["ledger"]["charge_usd"]) for run in available),
                Decimal(0),
            )
            if complete
            else None
        )
        low_rows.append(
            {
                "harness": harness,
                "status": "complete" if complete else "incomplete",
                "registered_trajectories": len(runs),
                "completed_trajectories": len(available),
                "verified_at_cost": counts,
                "single_run_rate_percent": 100 * counts["2"] / len(runs) if complete else None,
                "charge_usd": str(charge) if charge is not None else None,
                "charge_per_successful_trajectory": _ratio(charge, counts["2"])
                if complete
                else None,
            }
        )
    for problem in selected:
        row: dict[str, Any] = {"problem": problem}
        for harness in HARNESSES:
            runs = [
                run
                for run in manifest["runs"]
                if run["model"] == GEMINI
                and run["harness"] == harness
                and run["problem"] == problem
            ]
            available = [endpoints[run["run_id"]] for run in runs if run["run_id"] in endpoints]
            complete = len(available) == len(runs) and all(
                endpoint["ledger"]["accounting_complete"] for endpoint in available
            )
            row[f"{harness}_successes"] = (
                sum(endpoint["checkpoints"]["2"] for endpoint in available) if complete else None
            )
            row[f"{harness}_charge_usd"] = (
                str(
                    sum(
                        (Decimal(endpoint["ledger"]["charge_usd"]) for endpoint in available),
                        Decimal(0),
                    )
                )
                if complete
                else None
            )
        problem_rows.append(row)

    extended_rows = []
    for problem in (problem for problem in selected if problem in {"A5", "B6"}):
        for harness in HARNESSES:
            run = next(
                run
                for run in manifest["runs"]
                if run["extendable"] and run["problem"] == problem and run["harness"] == harness
            )
            run_id = run["run_id"]
            low, final = endpoints.get(run_id), final_endpoints.get(run_id)
            complete = (
                final is not None
                and final["ledger"]["accounting_complete"]
                and low["ledger"]["accounting_complete"]
            )
            snapshots = states[run_id]["snapshots"][: final["snapshot_count"]] if complete else []
            checkpoints = {
                str(threshold): any(
                    _eligible(snapshot, threshold, "extended", run) for snapshot in snapshots
                )
                if complete
                else None
                for threshold in EXTENDED_CHECKPOINTS
            }
            first_qualifying = next(
                (
                    snapshot
                    for snapshot in snapshots
                    if _eligible(snapshot, Decimal(50), "extended", run)
                ),
                None,
            )
            extended_rows.append(
                {
                    "run_id": run_id,
                    "problem": problem,
                    "harness": harness,
                    "status": "complete" if complete else "incomplete",
                    "continued": final["phase"] == "extended" if complete else None,
                    "low_resumable": low.get("resumable", True) if low is not None else None,
                    "qualifying_artifact_predates_low_freeze": first_qualifying["index"]
                    < low["snapshot_count"]
                    if first_qualifying is not None
                    else None,
                    "low_verified": low["verified"] if low is not None else None,
                    "verified": checkpoints["50"],
                    "new_success_after_low": bool(checkpoints["50"] and not low["verified"])
                    if complete
                    else None,
                    "verified_at_cost": checkpoints,
                    "charge_usd": final["ledger"]["charge_usd"] if complete else None,
                    "additional_continuation_charge_usd": str(
                        Decimal(final["ledger"]["charge_usd"])
                        - Decimal(low["ledger"]["charge_usd"])
                    )
                    if complete
                    else None,
                    "calls": final["ledger"]["calls"] if complete else None,
                    "active_hours": final["active_seconds"] / 3600 if complete else None,
                    "stop_reason": final["stop_reason"] if complete else None,
                }
            )

    anchor_runs = [run for run in manifest["runs"] if run["model"] == OPUS]
    available_anchors = [
        endpoints[run["run_id"]] for run in anchor_runs if run["run_id"] in endpoints
    ]
    anchor_complete = len(available_anchors) == len(anchor_runs) and all(
        endpoint["ledger"]["accounting_complete"] for endpoint in available_anchors
    )
    anchor = {
        "model": OPUS,
        "harness": "mini",
        "registered_trajectories": len(anchor_runs),
        "completed_trajectories": len(available_anchors),
        "status": "complete" if anchor_complete else "incomplete",
        "verified_at_cost": {
            str(threshold): sum(
                endpoint["checkpoints"][str(threshold)] for endpoint in available_anchors
            )
            if anchor_complete
            else None
            for threshold in ANCHOR_CHECKPOINTS
        },
        "charge_usd": str(
            sum(
                (Decimal(endpoint["ledger"]["charge_usd"]) for endpoint in available_anchors),
                Decimal(0),
            )
        )
        if anchor_complete
        else None,
    }
    low_complete = all(row["status"] == "complete" for row in low_rows)
    extended_complete = all(row["status"] == "complete" for row in extended_rows)
    low_charge = (
        sum((Decimal(row["charge_usd"]) for row in low_rows), Decimal(0)) if low_complete else None
    )
    continuation_charge = (
        sum(
            (Decimal(row["additional_continuation_charge_usd"]) for row in extended_rows),
            Decimal(0),
        )
        if extended_complete
        else None
    )
    total_charge = (
        low_charge + continuation_charge + Decimal(anchor["charge_usd"])
        if low_complete and extended_complete and anchor_complete
        else None
    )
    failures = [
        {"run_id": row["run_id"], "phase": row["phase"], "charge_usd": row["charge_usd"]}
        for row in per_run
        if row["stop_reason"] == "infrastructure_failure"
    ]
    annotations = (
        read_annotations(annotations_path, manifest) if annotations_path is not None else {}
    )
    return {
        "schema_version": 2,
        "scope": manifest["scope"],
        "status": "incomplete" if problems else "complete",
        "manifest_sha256": manifest["manifest_sha256"],
        "missing_or_incomplete": problems,
        "nominal_allocation_usd": manifest["nominal_allocation_usd"],
        "low_budget": low_rows,
        "per_problem": problem_rows,
        "extended": extended_rows,
        "historical_anchor": anchor,
        "realized_charge_usd": {
            "low": str(low_charge) if low_charge is not None else None,
            "additional_continuation": str(continuation_charge)
            if continuation_charge is not None
            else None,
            "anchor": anchor["charge_usd"],
            "total": str(total_charge) if total_charge is not None else None,
        },
        "overshoot": {
            phase: {
                "affected_phase_endpoints": sum(
                    row["charge_usd"] is not None and Decimal(row["charge_usd"]) > cap
                    for row in per_run
                    if row["phase"] == phase
                ),
                "charge_above_nominal_cap_usd": str(
                    sum(
                        (
                            max(Decimal(0), Decimal(row["charge_usd"]) - cap)
                            for row in per_run
                            if row["phase"] == phase and row["charge_usd"] is not None
                        ),
                        Decimal(0),
                    )
                ),
            }
            for phase, cap in (
                ("low", Decimal(2)),
                ("extended", Decimal(50)),
                ("anchor", Decimal(5)),
            )
        },
        "infrastructure_failure_sensitivity": {
            "policy": "All registered infrastructure failures remain in the primary denominators; only cost-qualified accepted snapshots count as successes. No failed runs are excluded.",
            "affected_endpoints": failures,
        },
        "trace_statistics": annotation_statistics(manifest, low_success, annotations),
        "per_run": per_run,
        "separate_costs": "Smoke tests, aborted setup, retrieval/embedding/indexing, evaluator compute, hosting, and invoice reconciliation must be reported separately; absent costs are not zero.",
    }


def _latex(value: Any) -> str:
    if value is None:
        return r"\textsf{TBD}"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    replacements = {
        "\\": r"\textbackslash{}",
        "&": r"\&",
        "%": r"\%",
        "$": r"\$",
        "#": r"\#",
        "_": r"\_",
        "{": r"\{",
        "}": r"\}",
        "~": r"\textasciitilde{}",
        "^": r"\textasciicircum{}",
    }
    return "".join(replacements.get(character, character) for character in str(value))


def _money(value: str | None) -> str | None:
    return None if value is None else f"${Decimal(value):.2f}"


def _fraction(value: int | None, denominator: int) -> str | None:
    return f"{value}/{denominator}" if value is not None else None


def _table(
    output_dir: Path, name: str, headers: list[str], rows: list[list[Any]], caption: str
) -> None:
    with (output_dir / f"{name}.csv").open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(headers)
        writer.writerows(rows)
    lines = [
        r"\begin{table}[ht]",
        r"\begingroup\color{blue}\centering\small",
        r"\caption{" + _latex(caption) + "}",
        r"\begin{tabular}{@{}" + "l" * len(headers) + "@{}}",
        r"\toprule",
        " & ".join(_latex(header) for header in headers) + r" \\",
        r"\midrule",
    ]
    lines.extend(" & ".join(_latex(value) for value in row) + r" \\" for row in rows)
    lines.extend([r"\bottomrule", r"\end{tabular}", r"\endgroup", r"\end{table}", ""])
    (output_dir / f"{name}.tex").write_text("\n".join(lines))


def write_report(report: dict[str, Any], output_dir: Path) -> None:
    """Export the paper's primary tables, raw accounting, and explicit pending status."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    scope = report["scope"]
    scope_label = (
        f"Full benchmark: {len(scope['problems'])} problems"
        if scope["kind"] == "full_benchmark"
        else f"Selected-problem pilot: {', '.join(scope['problems'])} "
        f"({len(scope['problems'])} of {scope['benchmark_problem_count']} benchmark problems)"
    )
    names = {"mini": "mini-Lean-agent", "axprover": "AxProverBase"}
    _table(
        output_dir,
        "low-budget",
        ["Harness", "$0.50", "$1", "$2", "Charge"],
        [
            [
                names[row["harness"]],
                *[
                    _fraction(
                        row["verified_at_cost"][str(threshold)], row["registered_trajectories"]
                    )
                    for threshold in LOW_CHECKPOINTS
                ],
                _money(row["charge_usd"]),
            ]
            for row in report["low_budget"]
        ],
        scope_label
        + ". Low-budget Gemini results from one trajectory per problem and harness. Charges include failed runs and exclude continuation spending; pending cells are not failures or zero spending.",
    )
    _table(
        output_dir,
        "per-problem",
        ["Problem", "mini success", "mini charge", "Ax success", "Ax charge"],
        [
            [
                row["problem"],
                _fraction(row["mini_successes"], 1),
                _money(row["mini_charge_usd"]),
                _fraction(row["axprover_successes"], 1),
                _money(row["axprover_charge_usd"]),
            ]
            for row in report["per_problem"]
        ],
        scope_label
        + ". Per-problem low-budget Gemini results out of one attempt. Later designated continuation outcomes and charges are excluded.",
    )
    _table(
        output_dir,
        "extended",
        ["Task", "Harness", "Verified", "Charge", "Calls", "Hours"],
        [
            [
                row["problem"],
                names[row["harness"]],
                row["verified"],
                _money(row["charge_usd"]),
                row["calls"],
                f"{row['active_hours']:.1f}" if row["active_hours"] is not None else None,
            ]
            for row in report["extended"]
        ],
        scope_label
        + ". Predeclared extended trajectories. Charges, calls, and active hours are cumulative, including the frozen low-budget prefix.",
    )
    _table(
        output_dir,
        "hard-checkpoints",
        ["Task", "Harness", "Low", "$5", "$10", "$25", "$50"],
        [
            [
                row["problem"],
                names[row["harness"]],
                row["low_verified"],
                *[row["verified_at_cost"][str(threshold)] for threshold in EXTENDED_CHECKPOINTS],
            ]
            for row in report["extended"]
        ],
        scope_label
        + f". Cost-qualified acceptance of the {len(report['extended'])} predeclared trajectories. Low uses its frozen safeguards; later checkpoints use cumulative extended safeguards.",
    )
    anchor = report["historical_anchor"]
    _table(
        output_dir,
        "historical",
        ["System", "Verified", "Provenance"],
        [
            [
                "mini, Opus 4.5",
                _fraction(anchor["verified_at_cost"]["2"], anchor["registered_trajectories"]),
                "$2 checkpoint",
            ],
            [
                "mini, Opus 4.5",
                _fraction(anchor["verified_at_cost"]["5"], anchor["registered_trajectories"]),
                "Same runs, $5 endpoint",
            ],
        ]
        + (
            [["Numina full system", "12/12", "Self-reported; not rerun"]]
            if scope["kind"] == "full_benchmark"
            else []
        ),
        scope_label
        + ". Historical model context, not a matched comparison. Opus checkpoints refer to the same registered trajectories.",
    )
    _table(
        output_dir,
        "charges",
        ["Phase", "Model charge"],
        [[phase, _money(value)] for phase, value in report["realized_charge_usd"].items()],
        scope_label
        + ". Realized model charges. The continuation row is additional spending, so low-budget prefixes are not counted twice. Other operating costs are separate.",
    )
    with (output_dir / "per-run.csv").open("w", newline="") as stream:
        fields = sorted({key for row in report["per_run"] for key in row}) or ["run_id", "phase"]
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(report["per_run"])
    (output_dir / "annotation-rubric.md").write_text(ANNOTATION_RUBRIC)
    text = [
        f"Experiment status: {report['status']}",
        scope_label,
        f"Manifest SHA-256: {report['manifest_sha256']}",
        "",
    ]
    for row in report["low_budget"]:
        for metric in ("charge_per_successful_trajectory",):
            value = row[metric]
            rendered = (
                "pending"
                if value is None
                else value["status"]
                if value["usd"] is None
                else _money(value["usd"])
            )
            text.append(f"{names[row['harness']]} {metric}: {rendered}.")
    text.extend(
        ["", report["infrastructure_failure_sensitivity"]["policy"], report["separate_costs"]]
    )
    if report["missing_or_incomplete"]:
        text.extend(["", "Missing or incomplete records:", *report["missing_or_incomplete"]])
    (output_dir / "summary.txt").write_text("\n".join(text) + "\n")
