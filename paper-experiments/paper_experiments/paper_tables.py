"""Reproduce paper tables without modifying registered experiments or their outcomes."""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import Any

from .ledger import Ledger
from .protocol import (
    ANCHOR_CHECKPOINTS,
    EXTENDED_CHECKPOINTS,
    GEMINI,
    LOW_CHECKPOINTS,
    MINI_CAMPAIGN_CHECKPOINTS,
    MINI_CAMPAIGN_MODELS,
    OPUS,
    PROBLEMS,
    Limits,
    content_hash,
    validate_registration,
)
from .reporting import (
    FAILURE_CATEGORIES,
    _load_run,
    annotation_statistics,
    read_annotations,
)

POLICIES = ("strict", "last-dispatch")
NAMES = {"mini": "mini-Lean-agent", "axprover": "AxProverBase"}
REPLACEMENT_RUN_ID = "gemini-3.8-flash--mini--B2--r1"
REPLACEMENT_RUN_IDS = {REPLACEMENT_RUN_ID, "gemini-3.8-flash--axprover--B3--r1"}
CAMPAIGN_NAMES = {"astra": "Astra", "fable": "Fable 5.1"}


def require(condition: Any, message: str) -> None:
    if not condition:
        raise ValueError(message)


def amount(value: Any) -> Decimal:
    value = Decimal(str(value))
    require(value.is_finite() and value >= 0, "Invalid charge")
    return value


def total(values: Any) -> str | None:
    values = list(values)
    return None if any(v is None for v in values) else str(sum(map(amount, values), Decimal(0)))


def ratio(value: str | None, successes: int) -> str | None:
    return str(amount(value) / successes) if value is not None and successes else None


@dataclass
class RunData:
    run: dict[str, Any]
    state: dict[str, Any]
    directory: str
    initial_reconciled: str | None
    final_reconciled: str | None

    @property
    def initial(self) -> dict[str, Any]:
        return self.state.get("low_endpoint") or self.state["endpoint"]

    @property
    def final(self) -> dict[str, Any]:
        return self.state["endpoint"]


def checkpoint(state: dict[str, Any], threshold: str, phase: str, policy: str) -> bool:
    require(policy in POLICIES, "Unknown budget policy")
    limit = Limits.for_run(state["run"], phase)
    end = state.get("low_endpoint") if phase == "low" else state["endpoint"]
    require(end is not None, "Missing frozen endpoint")
    bound = amount(threshold)
    for snapshot in state["snapshots"][: end["snapshot_count"]]:
        charge = snapshot["charge_usd"]
        if not (
            charge is not None
            and snapshot["verification"].get("verified") is True
            and snapshot["calls"] <= limit.calls
            and snapshot["active_seconds"] <= limit.active_seconds
            and snapshot["verification_completed_seconds"] <= limit.active_seconds
        ):
            continue
        if amount(charge) <= bound:
            return True
        if policy == "last-dispatch" and bound == limit.dollars:
            count = snapshot["calls"]
            attempts = end["ledger"]["attempts"][:count]
            if (
                not count
                or len(attempts) != count
                or any(not a["completed"] or a.get("charge_usd") is None for a in attempts)
            ):
                continue
            before = total(a["charge_usd"] for a in attempts[:-1])
            after = total(a["charge_usd"] for a in attempts)
            require(amount(after) == amount(charge), "Snapshot and dispatch charges disagree")
            if amount(before) < bound:
                return True
    return False


def summarize(
    runs: list[RunData],
    annotations: dict[str, Any],
    *,
    policy: str,
    diagnostic: RunData | None = None,
    replacements: list[RunData] | None = None,
    replacement_annotation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    require(
        len(runs) == 36 and len({r.run["run_id"] for r in runs}) == 36, "Expected 36 unique runs"
    )
    require({r.run["problem"] for r in runs} == set(PROBLEMS), "Expected all twelve problems")
    selected = {}
    selections = []
    for data in replacements or []:
        run_id = data.run["run_id"]
        require(run_id in REPLACEMENT_RUN_IDS, "Only B2 mini and B3 Ax replacements are supported")
        original = next(row for row in runs if row.run["run_id"] == run_id)
        require(data.run == original.run, "Replacement differs from the registered run")
        require(run_id not in selected, "Duplicate replacement")
        selected[run_id] = data
        selections.append(
            {
                "run_id": run_id,
                "directory": data.directory,
                "selection": f"{data.run['problem']} {data.run['harness']} replacement",
            }
        )
    if diagnostic is not None:
        run_id = "gemini-3.8-flash--axprover--B6--r1"
        original = next(row for row in runs if row.run["run_id"] == run_id)
        require(diagnostic.run == original.run, "Only the completed B6 retry can replace this run")
        selected[run_id] = diagnostic
        selections.append(
            {"run_id": run_id, "directory": diagnostic.directory, "selection": "completed B6 retry"}
        )
    selected_data = [selected.get(data.run["run_id"], data) for data in runs]
    annotations = {run_id: note for run_id, note in annotations.items() if run_id not in selected}
    if replacement_annotation is not None:
        require(
            REPLACEMENT_RUN_ID in selected, "B2 replacement annotation requires its selected run"
        )
        annotations[REPLACEMENT_RUN_ID] = replacement_annotation
    rows, hard, changes = [], [], []
    for data in selected_data:
        run, state = data.run, data.state
        initial_phase = "low" if run["model"] == GEMINI else "anchor"
        thresholds = LOW_CHECKPOINTS if initial_phase == "low" else ANCHOR_CHECKPOINTS
        checks = {str(b): checkpoint(state, str(b), initial_phase, policy) for b in thresholds}
        row = {
            **run,
            "directory": data.directory,
            "checkpoints": checks,
            "initial_reconciled_charge_usd": data.initial_reconciled,
            "initial_known_charge_usd": data.initial["ledger"]["known_charge_usd"],
            "final_reconciled_charge_usd": data.final_reconciled,
            "final_known_charge_usd": data.final["ledger"]["known_charge_usd"],
            "calls": data.final["ledger"]["calls"],
            "active_seconds": data.final["active_seconds"],
            "stop_reason": data.final["stop_reason"],
        }
        rows.append(row)
        phases = [(initial_phase, thresholds)]
        if run["extendable"]:
            hard_checks = {
                str(b): checkpoint(state, str(b), "extended", policy) for b in EXTENDED_CHECKPOINTS
            }
            hard.append({**row, "verified": hard_checks["50"], "verified_at_cost": hard_checks})
            phases.append(("extended", EXTENDED_CHECKPOINTS))
        for phase, budgets in phases:
            for budget in budgets:
                strict = checkpoint(state, str(budget), phase, "strict")
                chosen = checkpoint(state, str(budget), phase, policy)
                if strict != chosen:
                    changes.append(
                        {
                            "run_id": run["run_id"],
                            "phase": phase,
                            "threshold_usd": str(budget),
                            "strict": strict,
                            "selected_policy": chosen,
                        }
                    )
    panels = []
    for model, harness, budget in [
        (GEMINI, "mini", "2"),
        (GEMINI, "axprover", "2"),
        (OPUS, "mini", "2"),
        (OPUS, "mini", "5"),
    ]:
        chosen = [r for r in rows if r["model"] == model and r["harness"] == harness]
        require(len(chosen) == 12, "Each panel needs twelve trajectories")
        successes = sum(r["checkpoints"][budget] for r in chosen)
        charge = total(r["initial_reconciled_charge_usd"] for r in chosen)
        known = total(r["initial_known_charge_usd"] for r in chosen)
        panels.append(
            {
                "model": model,
                "harness": harness,
                "threshold_usd": budget,
                "successes": successes,
                "denominator": 12,
                "rate_percent": 100 * successes / 12,
                "charge_usd": charge,
                "known_charge_usd": known,
                "charge_basis": "initial Gemini phase"
                if model == GEMINI
                else "full Opus $5 anchor",
                "charge_per_success_usd": ratio(charge, successes) if model == GEMINI else None,
                "known_charge_per_success_usd": ratio(known, successes)
                if model == GEMINI
                else None,
            }
        )
    cohorts = []
    for model, harness, label in [
        (GEMINI, "mini", "Gemini, mini-Lean-agent"),
        (GEMINI, "axprover", "Gemini, AxProverBase"),
        (OPUS, "mini", "Opus 4.5"),
    ]:
        chosen = [r for r in rows if r["model"] == model and r["harness"] == harness]
        cohorts.append(
            {
                "model": model,
                "harness": harness,
                "display_name": label,
                "charge_usd": total(r["final_reconciled_charge_usd"] for r in chosen),
                "known_charge_usd": total(r["final_known_charge_usd"] for r in chosen),
                "calls": sum(r["calls"] for r in chosen),
            }
        )
    low_success = {r["run_id"]: r["checkpoints"]["2"] for r in rows if r["model"] == GEMINI}
    return {
        "schema_version": 2,
        "budget_policy": policy,
        "budget_policy_scope": "Last-dispatch applies only at stopping caps; interior checkpoints and call/time limits remain strict.",
        "problem_count": 12,
        "selected_trajectories": len(rows),
        "selections": selections,
        "panels": panels,
        "runs": rows,
        "hard": sorted(hard, key=lambda r: (r["problem"], list(NAMES).index(r["harness"]))),
        "checkpoint_changes": changes,
        "trace": annotation_statistics({"runs": [r.run for r in runs]}, low_success, annotations),
        "costs": {
            "cohorts": cohorts,
            "reported_model_charge_usd": total(row["charge_usd"] for row in cohorts),
            "known_reported_model_charge_usd": total(row["known_charge_usd"] for row in cohorts),
            "scope": "Selected benchmark attempts, including continuations; excludes setup and smoke tests.",
        },
    }


def campaign_accounting(ledger: dict[str, Any]) -> dict[str, Any]:
    """Count provider charges, including upstream invoices for BYOK requests."""
    attempts = ledger["attempts"]
    require(len(attempts) == ledger["calls"], "Campaign ledger call count differs")
    charges, openrouter, upstream = [], [], []
    byok = 0
    for attempt in attempts:
        charge = attempt.get("charge_usd")
        if charge is None:
            charges.append(None)
            continue
        require(
            attempt["completed"] and attempt["charge_basis"] == "provider",
            "Expected measured provider billing",
        )
        metadata = attempt["response_metadata"]
        billing, usage = metadata["billing"], metadata["usage"]
        is_byok = billing["is_byok"]
        require(
            type(is_byok) is bool and usage["is_byok"] is is_byok, "Provider billing route differs"
        )
        router_charge = amount(usage["cost"])
        provider_charge = (
            amount(usage["cost_details"]["upstream_inference_cost"]) if is_byok else Decimal(0)
        )
        require(
            amount(charge) == router_charge + provider_charge == amount(billing["total_charge_usd"])
            and router_charge == amount(billing["openrouter_charge_usd"])
            and (not is_byok or provider_charge == amount(billing["upstream_inference_cost_usd"])),
            "Recorded billing omits or duplicates a provider charge",
        )
        charges.append(charge)
        openrouter.append(str(router_charge))
        upstream.append(str(provider_charge))
        byok += int(is_byok)
    known = total(value for value in charges if value is not None)
    require(amount(known) == amount(ledger["known_charge_usd"]), "Known campaign charge differs")
    complete = all(value is not None for value in charges)
    require(ledger["accounting_complete"] is complete, "Campaign accounting status differs")
    charge = total(charges)
    require(
        ledger["charge_usd"] == charge
        or (
            charge is not None
            and ledger["charge_usd"] is not None
            and amount(ledger["charge_usd"]) == amount(charge)
        ),
        "Campaign charge total differs",
    )
    return {
        "charge_usd": charge,
        "known_charge_usd": known,
        "accounting_complete": complete,
        "openrouter_charge_usd": total(openrouter),
        "byok_upstream_charge_usd": total(upstream),
        "byok_calls": byok,
    }


def summarize_campaign(
    campaign: str,
    runs: list[RunData],
    scheduler: list[dict[str, Any]],
    smoke: dict[str, Any],
    *,
    policy: str,
) -> dict[str, Any]:
    require(campaign in MINI_CAMPAIGN_MODELS, "Unknown modern campaign")
    require(
        len(runs) == len({row.run["run_id"] for row in runs}) == 12
        and {row.run["problem"] for row in runs} == set(PROBLEMS)
        and all(
            row.run["model"] == MINI_CAMPAIGN_MODELS[campaign] and row.run["harness"] == "mini"
            for row in runs
        ),
        "Expected twelve unique mini campaign problems",
    )
    require(
        scheduler and all(row.get("finished_at") for row in scheduler),
        "Unfinished campaign scheduler",
    )
    results = [result for receipt in scheduler for result in receipt["results"]]
    require(
        len(results) == 12
        and {result["run_id"] for result in results} == {row.run["run_id"] for row in runs}
        and all(result.get("error") is None for result in results),
        "Incomplete campaign scheduler results",
    )
    started = min(datetime.fromisoformat(receipt["started_at"]) for receipt in scheduler)
    finished = max(datetime.fromisoformat(receipt["finished_at"]) for receipt in scheduler)
    require(
        started.tzinfo and finished.tzinfo and finished >= started,
        "Invalid campaign scheduler times",
    )
    rows = []
    for data in sorted(runs, key=lambda row: PROBLEMS.index(row.run["problem"])):
        state, endpoint = data.state, data.final
        require(
            state["paused"] and endpoint["phase"] == campaign, "Unfinished or wrong campaign phase"
        )
        accounting = campaign_accounting(endpoint["ledger"])
        checks = {
            str(value): checkpoint(state, str(value), campaign, policy)
            for value in MINI_CAMPAIGN_CHECKPOINTS
        }
        rows.append(
            {
                **data.run,
                "directory": data.directory,
                "verified": checks["50"],
                "checkpoints": checks,
                **accounting,
                "calls": endpoint["ledger"]["calls"],
                "active_seconds": endpoint["active_seconds"],
                "stop_reason": endpoint["stop_reason"],
                "tokens": endpoint["ledger"]["tokens"],
                "infrastructure_failures": sum(
                    bool(snapshot["verification"].get("infrastructure_failure"))
                    for snapshot in state["snapshots"]
                ),
            }
        )
    successes = sum(row["verified"] for row in rows)
    charge, known = (
        total(row["charge_usd"] for row in rows),
        total(row["known_charge_usd"] for row in rows),
    )
    require(smoke.get("ok") is True, "Campaign smoke did not pass")
    smoke_accounts = [campaign_accounting(case["ledger"]) for case in smoke["cases"]]
    require(
        all(row["accounting_complete"] for row in smoke_accounts), "Incomplete smoke accounting"
    )
    return {
        "campaign": campaign,
        "model": MINI_CAMPAIGN_MODELS[campaign],
        "display_name": CAMPAIGN_NAMES[campaign],
        "denominator": 12,
        "successes": successes,
        "charge_usd": charge,
        "known_charge_usd": known,
        "charge_per_success_usd": ratio(charge, successes),
        "known_charge_per_success_usd": ratio(known, successes),
        "accounting_complete": all(row["accounting_complete"] for row in rows),
        "openrouter_charge_usd": total(row["openrouter_charge_usd"] for row in rows),
        "byok_upstream_charge_usd": total(row["byok_upstream_charge_usd"] for row in rows),
        "byok_calls": sum(row["byok_calls"] for row in rows),
        "calls": sum(row["calls"] for row in rows),
        "active_seconds": sum(row["active_seconds"] for row in rows),
        "wall_seconds": (finished - started).total_seconds(),
        "wall_time_basis": "First scheduler start through final scheduler completion; excludes smoke and predecessor waiting",
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "checkpoints": {
            str(value): sum(row["checkpoints"][str(value)] for row in rows)
            for value in MINI_CAMPAIGN_CHECKPOINTS
        },
        "infrastructure_failures": sum(row["infrastructure_failures"] for row in rows),
        "smoke_charge_usd": total(row["charge_usd"] for row in smoke_accounts),
        "runs": rows,
    }


def add_campaigns(model: dict[str, Any], campaigns: list[dict[str, Any]]) -> None:
    if not campaigns:
        return
    require(len({row["campaign"] for row in campaigns}) == len(campaigns), "Duplicate campaign")
    model["budget_policy_scope"] += (
        " Standalone campaigns apply the same allowance only at their $50 stopping cap."
    )
    model["campaigns"] = sorted(
        campaigns, key=lambda row: list(CAMPAIGN_NAMES).index(row["campaign"])
    )
    costs = model["costs"]
    costs["reported_model_charge_usd"] = total(
        [costs["reported_model_charge_usd"], *[row["charge_usd"] for row in campaigns]]
    )
    costs["known_reported_model_charge_usd"] = total(
        [costs["known_reported_model_charge_usd"], *[row["known_charge_usd"] for row in campaigns]]
    )
    costs["cohorts"].extend(
        {
            key: row[key]
            for key in ("model", "display_name", "charge_usd", "known_charge_usd", "calls")
        }
        | {"harness": "mini"}
        for row in campaigns
    )


class Evidence:
    def __init__(self) -> None:
        self.files: dict[str, str] = {}

    def bytes(self, path: Path, expected: str | None = None) -> bytes:
        path = path.resolve()
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        require(expected is None or digest == expected, f"Changed evidence: {path}")
        require(
            str(path) not in self.files or self.files[str(path)] == digest,
            f"Evidence changed while reading: {path}",
        )
        self.files[str(path)] = digest
        return data

    def read(self, path: Path, expected: str | None = None) -> Any:
        return json.loads(self.bytes(path, expected))


def load_frozen(run: dict[str, Any], directory: Path, evidence: Evidence) -> dict[str, Any]:
    state = _load_run(run, directory)
    require(
        state is not None and state.get("paused") is True and state.get("endpoint"),
        "Unfinished trajectory",
    )
    require(
        evidence.read(directory / "controller.json") == state,
        "Controller changed during validation",
    )
    final = state["endpoint"]
    low = state.get("low_endpoint")
    require(
        not (
            run["extendable"]
            and final["phase"] == "low"
            and low["resumable"]
            and not low["verified"]
            and low["stop_reason"] in {"cost", "call_count", "elapsed_time"}
        ),
        "Missing required continuation",
    )
    require(final["snapshot_count"] == len(state["snapshots"]), "Snapshots after terminal freeze")
    events = [
        json.loads(line)
        for line in evidence.bytes(directory / "ledger.jsonl").splitlines()
        if line.strip()
    ]
    ledger = final["ledger"]
    require(content_hash(events) == ledger["journal_sha256"], "Final ledger journal changed")
    require(Ledger._attempts(events) == ledger["attempts"], "Final ledger attempts changed")
    require(len(ledger["attempts"]) == ledger["calls"], "Ledger call count differs")
    for endpoint in [final, *([low] if low else [])]:
        evidence.read(directory / f"{endpoint['phase']}-endpoint.json")
    for snapshot in state["snapshots"]:
        folder = directory / "snapshots" / f"{snapshot['index']:06d}"
        evidence.read(folder / "snapshot.json")
        evidence.bytes(folder / "candidate.lean", snapshot["sha256"])
    return state


def supplemental_charge(
    row: dict[str, Any],
    state: dict[str, Any],
    directory: Path,
    evidence: Evidence,
    settings: dict[str, Any] | None = None,
) -> str | None:
    directory = directory.resolve()
    ledger = state["endpoint"]["ledger"]
    if ledger["accounting_complete"]:
        return ledger["charge_usd"]
    approved = row.get("accounting_exception")
    require(
        approved
        and state["endpoint"]["stop_reason"] == "infrastructure_failure"
        and not state["endpoint"]["resumable"],
        "Unapproved incomplete accounting",
    )
    require(
        not any(s["verification"].get("verified") for s in state["snapshots"]),
        "Accounting exception cannot grant a proof",
    )
    evidence.bytes(directory / f"{state['phase']}-endpoint.json", approved["endpoint_sha256"])
    receipt = evidence.read(Path(approved["supplement_path"]), approved["supplement_sha256"])
    require(receipt["run_id"] == row["run_id"], "Supplement belongs to another run")
    for path, expected in receipt["primary_hashes"].items():
        evidence.bytes(Path(path), expected)
    for name in ["controller.json", "ledger.jsonl", f"{state['phase']}-endpoint.json"]:
        require(
            str(directory / name) in receipt["primary_hashes"], "Supplement omits primary binding"
        )
    summary = receipt["summary"]
    unknown = [a["attempt_id"] for a in ledger["attempts"] if a.get("charge_usd") is None]
    require(
        summary["primary_accounting_complete"] is False
        and summary["primary_total_charge_usd"] is None,
        "Supplement replaced primary accounting",
    )
    require(
        summary["unresolved_primary_attempts"] == unknown
        and summary["registered_calls"] == ledger["calls"],
        "Supplement attempt scope changed",
    )
    require(
        amount(summary["primary_known_charge_usd"]) == amount(ledger["known_charge_usd"]),
        "Supplement known subtotal differs",
    )
    resolutions = receipt["resolutions"]
    require(
        sorted(r["attempt_id"] for r in resolutions) == unknown, "Supplement resolutions differ"
    )
    if any(r["status"] != "reconciled" for r in resolutions):
        require(
            summary["reconciled_total_charge_usd"] is None
            and summary["supplemental_accounting_complete"] is False,
            "Pending charge fabricated",
        )
        return None
    for resolution in resolutions:
        generation = resolution["generation"]
        require(
            content_hash(generation) == resolution["generation_sha256"], "Provider receipt changed"
        )
        require(
            generation["id"] == resolution["response_id"]
            and type(generation.get("is_byok")) is bool,
            "Provider receipt identity or billing mode missing",
        )
        provider_charge = amount(generation["total_cost"]) + (
            amount(generation["upstream_inference_cost"]) if generation["is_byok"] else Decimal(0)
        )
        require(
            provider_charge == amount(resolution["receipt_charge_usd"]),
            "Provider receipt charge differs",
        )
        attempt = ledger["attempts"][resolution["attempt_id"] - 1]
        require(
            attempt["attempt_id"] == resolution["attempt_id"], "Unknown attempt identity differs"
        )
        if resolution.get("record_path"):
            require(
                resolution["record_path"] in receipt["primary_hashes"],
                "Provider archive lacks primary binding",
            )
            archive = evidence.read(Path(resolution["record_path"]))
            require(
                archive["attempt_id"] == resolution["attempt_id"],
                "Provider archive belongs to another attempt",
            )
            require(
                content_hash(archive["request"])
                == archive["request_sha256"]
                == attempt["metadata"]["request_sha256"],
                "Provider request binding differs",
            )
            require(
                content_hash(archive["response"])
                == archive["response_sha256"]
                == resolution["response_sha256"],
                "Provider response binding differs",
            )
            recorded_id = archive["response"]["data"]["response_metadata"]["id"]
        else:
            recorded_id = attempt["response_metadata"].get("response_id")
        require(
            recorded_id == resolution["response_id"],
            "Receipt belongs to another dispatched generation",
        )
        if settings:
            require(generation["model"] == settings["canonical_model"], "Receipt model differs")
            provider = generation["provider_name"].lower().replace(" ", "-")
            require(
                provider in settings["openrouter_provider"]["only"]
                and generation.get("service_tier") == "default",
                "Receipt provider or service tier differs",
            )
    charge = total([ledger["known_charge_usd"], *[r["receipt_charge_usd"] for r in resolutions]])
    require(
        summary["supplemental_accounting_complete"] is True
        and amount(charge) == amount(summary["reconciled_total_charge_usd"]),
        "Reconciled total differs",
    )
    return charge


def load_inputs(
    combined_path: Path,
    annotation_path: Path,
    provenance_path: Path,
    diagnostic_path: Path | None = None,
    diagnostic_supplement: Path | None = None,
    diagnostic_supplement_sha256: str | None = None,
) -> tuple[list[RunData], dict[str, Any], RunData | None, Evidence]:
    evidence = Evidence()
    combined = evidence.read(combined_path)
    require(
        combined["execution_complete"]
        and combined["outcomes_complete"]
        and combined["scope"]["runs"] == 36,
        "Combined report is unfinished",
    )
    require(len(combined["cohorts"]) == 3, "Expected three original registrations")
    rows = {r["run_id"]: r for r in combined["per_run"]}
    require(len(rows) == len(combined["per_run"]) == 36, "Duplicate or missing original runs")
    runs, annotations, manifests = [], {}, {}
    annotation_provenance = evidence.read(provenance_path)
    registered_inputs = []
    for cohort in combined["cohorts"]:
        directory = Path(cohort["directory"])
        manifest = evidence.read(directory / "runs.json", cohort["manifest_file_sha256"])
        inputs = evidence.read(directory / "experiment.json")
        require(content_hash(inputs) == cohort["experiment_sha256"], "Combined input hash differs")
        require(
            manifest == cohort["manifest"] and inputs == cohort["inputs"],
            "Combined cohort was changed",
        )
        validate_registration(manifest)
        require(
            content_hash(manifest) == inputs["registration_sha256"], "Original registration changed"
        )
        require(
            content_hash(inputs) == evidence.read(directory / "inputs.sha256.json")["sha256"],
            "Frozen inputs changed",
        )
        require(
            content_hash(inputs["source_files"]) == inputs["source_sha256"],
            "Original source fingerprint differs",
        )
        for relative, digest in inputs["source_files"].items():
            evidence.bytes(directory / "source" / relative, digest)
        registered_inputs.append(inputs)
        manifests[cohort["name"]] = manifest
        csv_record = (
            annotation_provenance["unchanged_a1_annotation_csv"]
            if cohort["name"] == "a1"
            else annotation_provenance["generated_csvs"][cohort["name"]]
        )
        csv_path = Path(csv_record["path"])
        if not csv_path.exists():
            csv_path = annotation_path.parent / csv_path.name
        evidence.bytes(csv_path, csv_record["sha256"])
        annotations.update(read_annotations(csv_path, manifest))
        for run in manifest["runs"]:
            run_dir = directory / "runs" / run["run_id"]
            state = load_frozen(run, run_dir, evidence)
            row = rows[run["run_id"]]
            require(
                row["cohort"] == cohort["name"]
                and row["final_calls"] == state["endpoint"]["ledger"]["calls"],
                "Combined run differs",
            )
            final = supplemental_charge(
                row, state, run_dir, evidence, inputs["models"][run["model"]]
            )
            initial = (state.get("low_endpoint") or state["endpoint"])["ledger"]["charge_usd"]
            if state["endpoint"]["phase"] != "extended":
                initial = final
            require(
                final == row["reconciled_charge_usd"]
                and initial == row["initial_reconciled_charge_usd"],
                "Combined financial row differs",
            )
            runs.append(RunData(run, state, str(run_dir), initial, final))
    for field in [
        "models",
        "prices",
        "bundle_manifest_sha256",
        "shared_task",
        "verification_timeout_seconds",
        "source_sha256",
    ]:
        require(
            all(i[field] == registered_inputs[0][field] for i in registered_inputs),
            f"Cohort {field} differs",
        )
    baseline = summarize(runs, annotations, policy="strict")
    trace = evidence.read(annotation_path)
    require(
        trace == annotation_provenance["trace_statistics"] == baseline["trace"],
        "Manual statistics changed",
    )
    diagnostic = None
    if diagnostic_path:
        diagnostic_path = diagnostic_path.resolve()
        manifest = evidence.read(diagnostic_path / "diagnostic-manifest.json")
        require(
            manifest["kind"] == "single_run_diagnostic_retry_not_benchmark_replacement",
            "Expected a separately labelled diagnostic",
        )
        run = manifest["run"]
        require(
            run["run_id"] == "gemini-3.8-flash--axprover--B6--r1",
            "Only the reviewed B6 Ax retry is supported",
        )
        inputs = evidence.read(diagnostic_path / "diagnostic-inputs.json")
        require(content_hash(inputs) == manifest["inputs_sha256"], "Diagnostic inputs changed")
        require(
            content_hash(inputs["source_files"]) == inputs["source_sha256"],
            "Diagnostic source fingerprint differs",
        )
        for relative, digest in inputs["source_files"].items():
            evidence.bytes(diagnostic_path / "source" / relative, digest)
        original = next(
            i
            for i in registered_inputs
            if i["registration_sha256"] == inputs["registration_sha256"]
        )
        for field in [
            "models",
            "prices",
            "bundle_manifest_sha256",
            "resources",
            "shared_task",
            "verification_timeout_seconds",
        ]:
            require(inputs[field] == original[field], f"Diagnostic {field} differs")
        require(
            inputs["retrieval"]["metadata"] == original["retrieval"]["metadata"],
            "Diagnostic retrieval differs",
        )
        run_dir = diagnostic_path / "runs" / run["run_id"]
        state = load_frozen(run, run_dir, evidence)
        charge = state["endpoint"]["ledger"]["charge_usd"]
        if not state["endpoint"]["ledger"]["accounting_complete"]:
            require(
                diagnostic_supplement and diagnostic_supplement_sha256,
                "Diagnostic accounting requires an explicitly hash-bound supplement",
            )
            charge = supplemental_charge(
                {
                    "run_id": run["run_id"],
                    "accounting_exception": {
                        "supplement_path": str(diagnostic_supplement),
                        "supplement_sha256": diagnostic_supplement_sha256,
                        "endpoint_sha256": hashlib.sha256(
                            (run_dir / f"{state['phase']}-endpoint.json").read_bytes()
                        ).hexdigest(),
                    },
                },
                state,
                run_dir,
                evidence,
                inputs["models"][run["model"]],
            )
        diagnostic = RunData(
            run, state, str(run_dir), state["low_endpoint"]["ledger"]["charge_usd"], charge
        )
    return runs, annotations, diagnostic, evidence


def load_campaign(
    directory: Path, campaign: str, evidence: Evidence, *, policy: str
) -> dict[str, Any]:
    directory = directory.resolve()
    manifest = evidence.read(directory / "runs.json")
    validate_registration(manifest)
    require(manifest.get("campaign") == campaign, "Wrong campaign registration")
    inputs = evidence.read(directory / "experiment.json")
    require(
        content_hash(manifest) == inputs["registration_sha256"], "Campaign registration changed"
    )
    require(
        content_hash(inputs) == evidence.read(directory / "inputs.sha256.json")["sha256"],
        "Frozen campaign inputs changed",
    )
    require(
        content_hash(inputs["source_files"]) == inputs["source_sha256"],
        "Campaign source fingerprint differs",
    )
    for relative, digest in inputs["source_files"].items():
        evidence.bytes(directory / "source" / relative, digest)
    rows = []
    for run in manifest["runs"]:
        run_dir = directory / "runs" / run["run_id"]
        state = load_frozen(run, run_dir, evidence)
        for snapshot in state["snapshots"]:
            verification = snapshot["verification"]
            require(
                verification["candidate_sha256"] == snapshot["sha256"],
                "Verification is bound to a different candidate",
            )
            require(
                verification["bundle_manifest_sha256"] == inputs["bundle_manifest_sha256"],
                "Verification used a different benchmark",
            )
        charge = state["endpoint"]["ledger"]["charge_usd"]
        rows.append(RunData(run, state, str(run_dir), charge, charge))
    scheduler = [evidence.read(path) for path in sorted((directory / "scheduler").glob("*.json"))]
    result = summarize_campaign(campaign, rows, scheduler, inputs["smoke"], policy=policy)
    result.update(
        experiment=str(directory),
        configuration=inputs["models"][MINI_CAMPAIGN_MODELS[campaign]],
        resources=inputs["resources"],
    )
    return result


def validate_replacement_manifest(
    manifest: dict[str, Any], original: RunData, started_at: str | None = None
) -> None:
    require(
        manifest["kind"] == "single_run_replacement_not_original_registration",
        "Expected an explicitly declared replacement",
    )
    require(
        manifest["run"] == original.run and original.run["run_id"] in REPLACEMENT_RUN_IDS,
        "Only B2 mini and B3 Ax replacements are supported",
    )
    require(
        manifest["selection_policy"]
        == f"replace {original.run['problem']}-{original.run['harness']} regardless of outcome",
        "Replacement selection must be independent of outcome",
    )
    require(
        manifest["phase"] == "low" and manifest["original_outcome_retained"] is True,
        "Replacement must retain original outcome",
    )
    require(
        manifest["limits"] == {"low": Limits.for_run(original.run, "low").to_dict()},
        "Replacement limits differ",
    )
    declared = datetime.fromisoformat(manifest["declared_at"])
    require(declared.utcoffset() is not None, "Replacement declaration needs a timezone")
    if started_at:
        started = datetime.fromisoformat(started_at)
        require(
            started.utcoffset() is not None and declared <= started,
            "Replacement was declared after execution started",
        )


def load_replacement(
    path: Path, manifest_sha256: str, runs: list[RunData], evidence: Evidence
) -> RunData:
    path = path.resolve()
    manifest = evidence.read(path / "replacement-manifest.json", manifest_sha256)
    run_id = manifest["run"]["run_id"]
    require(run_id in REPLACEMENT_RUN_IDS, "Unsupported replacement run")
    original = next(r for r in runs if r.run["run_id"] == run_id)
    validate_replacement_manifest(manifest, original)
    original_dir = Path(original.directory)
    require(
        Path(manifest["original_run_directory"]).resolve() == original_dir.resolve(),
        "Replacement refers to another original",
    )
    for name, field in [
        ("controller.json", "original_controller_sha256"),
        ("low-endpoint.json", "original_endpoint_sha256"),
        ("ledger.jsonl", "original_ledger_sha256"),
    ]:
        evidence.bytes(original_dir / name, manifest[field])
    original_inputs = evidence.read(original_dir.parent.parent / "experiment.json")
    inputs = evidence.read(path / "replacement-inputs.json")
    require(content_hash(inputs) == manifest["inputs_sha256"], "Replacement inputs changed")
    for field in [
        "models",
        "prices",
        "bundle",
        "bundle_manifest_sha256",
        "resources",
        "shared_task",
        "verification_timeout_seconds",
        "source_files",
        "source_sha256",
        "sdk_versions",
        "dependency_cache",
    ]:
        require(inputs[field] == original_inputs[field], f"Replacement {field} differs")
    require(
        content_hash(inputs["source_files"])
        == inputs["source_sha256"]
        == manifest["source_sha256"],
        "Replacement source fingerprint differs",
    )
    for relative, digest in inputs["source_files"].items():
        evidence.bytes(path / "source" / relative, digest)
    require(
        inputs["retrieval"]["metadata"] == original_inputs["retrieval"]["metadata"],
        "Replacement retrieval differs",
    )
    retrieval = Path(inputs["retrieval"]["path"])
    require(
        evidence.read(retrieval / "metadata.json") == inputs["retrieval"]["metadata"],
        "Replacement retrieval archive changed",
    )
    evidence.bytes(retrieval / "openapi.json", inputs["retrieval"]["metadata"]["api_schema_sha256"])
    run_dir = path / "runs" / run_id
    require(
        run_dir.resolve() != original_dir.resolve(),
        "Replacement cannot reuse original run directory",
    )
    state = load_frozen(original.run, run_dir, evidence)
    validate_replacement_manifest(manifest, original, state["started_at"])
    charge = state["endpoint"]["ledger"]["charge_usd"]
    return RunData(original.run, state, str(run_dir), charge, charge)


def load_replacement_annotation(
    path: Path,
    replacement: RunData,
    evidence: Evidence,
    expected_sha256: str | None = None,
) -> dict[str, Any]:
    note = evidence.read(path, expected_sha256)
    row = note["row"]
    require(row["run_id"] == REPLACEMENT_RUN_ID, "Annotation belongs to another run")
    bound = set()

    def validate_records(value):
        if isinstance(value, dict):
            if "path" in value and "sha256" in value:
                record_path = Path(value["path"])
                evidence.bytes(record_path, value["sha256"])
                bound.add(record_path.resolve())
            for child in value.values():
                validate_records(child)
        elif isinstance(value, list):
            for child in value:
                validate_records(child)

    validate_records(note["evidence"])
    root = Path(replacement.directory).resolve()
    require(
        root / "low-endpoint.json" in bound, "Replacement annotation must bind its own endpoint"
    )
    require(
        any(p.is_relative_to(root / "snapshots") and p.name == "driver-state.json" for p in bound),
        "Replacement annotation must bind its own frozen trace",
    )
    require(
        type(row["mathlib_search"]) is bool and type(row["scratch_files"]) is bool,
        "Annotation search/scratch values must be boolean",
    )
    require(
        row["first_lean_check_failed"] is None or type(row["first_lean_check_failed"]) is bool,
        "Invalid first-check annotation",
    )
    require(
        type(row["lean_invocations"]) is int
        and row["lean_invocations"] >= 0
        and row["failure_category"] in FAILURE_CATEGORIES
        and bool(row["evidence"]),
        "Invalid manual annotation",
    )
    return row


def escape(value: Any) -> str:
    return "".join(
        {
            "\\": r"\textbackslash{}",
            "&": r"\&",
            "%": r"\%",
            "$": r"\$",
            "#": r"\#",
            "_": r"\_",
            "{": r"\{",
            "}": r"\}",
        }.get(c, c)
        for c in str(value)
    )


def money(value: str | None, known: str | None = None) -> str:
    if value is not None:
        return r"\$" + f"{amount(value):.6f}"
    if known is not None:
        return r"\(\geq\)\$" + str(amount(known).quantize(Decimal("0.000001"), rounding=ROUND_DOWN))
    return r"\textsf{unavailable}"


def tabular(headers: list[str], rows: list[list[str]]) -> str:
    opening = r"\begin{tabular}{@{}l" + "c" * (len(headers) - 1) + "@{}}"
    return "\n".join(
        [
            opening,
            r"\toprule",
            " & ".join(headers) + r" \\",
            r"\midrule",
            *[" & ".join(row) + r" \\" for row in rows],
            r"\bottomrule",
            r"\end{tabular}",
            "",
        ]
    )


def campaign_money(value: str | None, known: str | None = None) -> str:
    if value is not None:
        return r"\$" + f"{amount(value):.2f}"
    if known is not None:
        return r"\(\geq\)\$" + str(amount(known).quantize(Decimal("0.01"), rounding=ROUND_DOWN))
    return r"\textsf{unavailable}"


def campaign_tables(campaigns: list[dict[str, Any]]) -> tuple[dict[str, str], dict[str, str]]:
    tables = {
        "high-budget": tabular(
            ["Model", "Verified", "Charge", "Charge/proof", "Calls", "Minutes"],
            [
                [
                    row["display_name"],
                    f"{row['successes']}/12",
                    campaign_money(row["charge_usd"], row["known_charge_usd"]),
                    campaign_money(
                        row["charge_per_success_usd"], row["known_charge_per_success_usd"]
                    ),
                    str(row["calls"]),
                    f"{row['wall_seconds'] / 60:.1f}",
                ]
                for row in campaigns
            ],
        ),
        "high-budget-checkpoints": tabular(
            ["Model", *[r"\$" + str(value) for value in MINI_CAMPAIGN_CHECKPOINTS]],
            [
                [
                    row["display_name"],
                    *[
                        f"{row['checkpoints'][str(value)]}/12"
                        for value in MINI_CAMPAIGN_CHECKPOINTS
                    ],
                ]
                for row in campaigns
            ],
        ),
    }
    by_problem = [{row["problem"]: row for row in panel["runs"]} for panel in campaigns]
    per_problem = tabular(
        ["Problem", *[heading for _ in campaigns for heading in ["Result", "USD", "Calls", "Min"]]],
        [
            [
                problem,
                *[
                    value
                    for mapping in by_problem
                    for value in [
                        "yes" if mapping[problem]["verified"] else "no",
                        campaign_money(
                            mapping[problem]["charge_usd"], mapping[problem]["known_charge_usd"]
                        ),
                        str(mapping[problem]["calls"]),
                        f"{mapping[problem]['active_seconds'] / 60:.2f}",
                    ]
                ],
            ]
            for problem in PROBLEMS
        ],
    )
    groups = (
        " & ".join(["", *[r"\multicolumn{4}{c}{" + row["display_name"] + "}" for row in campaigns]])
        + r" \\"
    )
    rules = " ".join(
        r"\cmidrule(lr){" + f"{2 + 4 * index}-{5 + 4 * index}" + "}"
        for index in range(len(campaigns))
    )
    tables["high-budget-per-problem"] = per_problem.replace(
        r"\toprule", r"\toprule" + "\n" + groups + "\n" + rules, 1
    )
    macros = {}
    for panel in campaigns:
        prefix = "Paper" + panel["campaign"].capitalize()
        macros.update(
            {
                prefix + "Successes": str(panel["successes"]),
                prefix + "Charge": campaign_money(panel["charge_usd"], panel["known_charge_usd"]),
                prefix + "CostPerProof": campaign_money(
                    panel["charge_per_success_usd"], panel["known_charge_per_success_usd"]
                ),
                prefix + "Calls": str(panel["calls"]),
                prefix + "Minutes": f"{panel['wall_seconds'] / 60:.2f}",
                prefix + "Hours": f"{panel['wall_seconds'] / 3600:.2f}",
            }
        )
        for name, threshold in zip(
            ["Two", "Five", "Ten", "Twenty", "Fifty"], MINI_CAMPAIGN_CHECKPOINTS, strict=True
        ):
            macros[prefix + "At" + name] = str(panel["checkpoints"][str(threshold)])
        for row in panel["runs"]:
            if row["problem"] not in {"A5", "B6"}:
                continue
            key = prefix + {"A5": "AFive", "B6": "BSix"}[row["problem"]]
            macros.update(
                {
                    key + "Charge": campaign_money(row["charge_usd"], row["known_charge_usd"]),
                    key + "Calls": str(row["calls"]),
                    key + "Minutes": f"{row['active_seconds'] / 60:.2f}",
                    key + "Verified": "yes" if row["verified"] else "no",
                }
            )
    return tables, macros


TABLE_MACROS = {
    "low-budget": "PaperLowBudgetTable",
    "per-problem": "PaperPerProblemTable",
    "extended": "PaperExtendedTable",
    "high-budget": "PaperHighBudgetTable",
    "high-budget-per-problem": "PaperHighBudgetPerProblemTable",
    "high-budget-checkpoints": "PaperHighBudgetCheckpointsTable",
    "charges": "PaperChargesTable",
}


def compact_results(model: dict[str, Any], evidence: Evidence) -> dict[str, Any]:
    row_fields = (
        "run_id",
        "problem",
        "model",
        "harness",
        "checkpoints",
        "initial_reconciled_charge_usd",
        "final_reconciled_charge_usd",
        "calls",
        "active_seconds",
        "stop_reason",
    )
    campaign_fields = (
        "campaign",
        "model",
        "denominator",
        "successes",
        "charge_usd",
        "charge_per_success_usd",
        "calls",
        "active_seconds",
        "wall_seconds",
        "checkpoints",
        "configuration",
        "resources",
    )
    campaigns = [
        {
            **{key: panel[key] for key in campaign_fields if key in panel},
            "runs": [
                {
                    key: row[key]
                    for key in (
                        "problem",
                        "verified",
                        "checkpoints",
                        "charge_usd",
                        "calls",
                        "active_seconds",
                        "stop_reason",
                    )
                }
                for row in panel["runs"]
            ],
        }
        for panel in model.get("campaigns", [])
    ]
    record_names = {
        "runs.json",
        "experiment.json",
        "replacement-manifest.json",
        "replacement-inputs.json",
        "diagnostic-manifest.json",
        "diagnostic-inputs.json",
    }
    return {
        "schema_version": 2,
        "problem_count": model["problem_count"],
        "budget_policy": model["budget_policy"],
        "budget_policy_scope": model["budget_policy_scope"],
        "panels": model["panels"],
        "runs": [{key: row[key] for key in row_fields} for row in model["runs"]],
        "campaigns": campaigns,
        "costs": model["costs"],
        "checkpoint_changes": model["checkpoint_changes"],
        "reproducibility": {
            "generator_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "validated_file_count": len(evidence.files),
            "validated_evidence_sha256": content_hash(evidence.files),
            "registrations": [
                {"path": path, "sha256": digest}
                for path, digest in sorted(evidence.files.items())
                if Path(path).name in record_names and "source" not in Path(path).parts
            ],
            "selected_retries": model["selections"],
            "selection_note": "Declared replacements enter the comparison regardless of outcome; archived registrations remain unchanged. The completed B6 retry supplies both reported phases.",
        },
    }


def write_tables(model: dict[str, Any], output: Path, evidence: Evidence) -> None:
    require(not output.exists(), "Output must be a fresh directory")
    require(
        not any(
            output.resolve() == Path(p).parent or output.resolve() in Path(p).parents
            for p in evidence.files
        ),
        "Output overlaps evidence",
    )
    rows, panels = model["runs"], model["panels"]
    by_key = {(r["problem"], r["model"], r["harness"]): r for r in rows}
    tables = {
        "low-budget": tabular(
            ["Harness", r"\$0.50", r"\$1", r"\$2", "Charge"],
            [
                [
                    escape(NAMES[p["harness"]]),
                    *[
                        f"{sum(r['checkpoints'][str(b)] for r in rows if r['model'] == GEMINI and r['harness'] == p['harness'])}/12"
                        for b in LOW_CHECKPOINTS
                    ],
                    money(p["charge_usd"], p["known_charge_usd"]),
                ]
                for p in panels[:2]
            ],
        ),
        "per-problem": tabular(
            ["Problem", "mini success", "mini charge", "Ax success", "Ax charge"],
            [
                [
                    problem,
                    *[
                        value
                        for harness in NAMES
                        for value in [
                            f"{int(by_key[problem, GEMINI, harness]['checkpoints']['2'])}/1",
                            money(
                                by_key[problem, GEMINI, harness]["initial_reconciled_charge_usd"],
                                by_key[problem, GEMINI, harness]["initial_known_charge_usd"],
                            ),
                        ]
                    ],
                ]
                for problem in PROBLEMS
            ],
        ),
        "extended": tabular(
            ["Task", "Harness", "Verified", "Charge", "Calls", "Hours"],
            [
                [
                    r["problem"],
                    escape(NAMES[r["harness"]]),
                    "yes" if r["verified"] else "no",
                    money(r["final_reconciled_charge_usd"], r["final_known_charge_usd"]),
                    str(r["calls"]),
                    f"{r['active_seconds'] / 3600:.3f}",
                ]
                for r in model["hard"]
            ],
        ),
    }
    costs = model["costs"]
    tables["charges"] = tabular(
        ["Campaign", "Model charge"],
        [
            *[
                [escape(row["display_name"]), money(row["charge_usd"], row["known_charge_usd"])]
                for row in costs["cohorts"]
            ],
            [
                "Total reported model charge",
                money(costs["reported_model_charge_usd"], costs["known_reported_model_charge_usd"]),
            ],
        ],
    )
    macros = {
        "PaperMiniSuccesses": str(panels[0]["successes"]),
        "PaperAxSuccesses": str(panels[1]["successes"]),
        "PaperMiniCostPerProof": campaign_money(
            panels[0]["charge_per_success_usd"], panels[0]["known_charge_per_success_usd"]
        ),
        "PaperAxCostPerProof": campaign_money(
            panels[1]["charge_per_success_usd"], panels[1]["known_charge_per_success_usd"]
        ),
        "PaperOpusCharge": campaign_money(panels[3]["charge_usd"], panels[3]["known_charge_usd"]),
    }
    campaigns = model.get("campaigns", [])
    if campaigns:
        additional_tables, additional_macros = campaign_tables(campaigns)
        tables.update(additional_tables)
        used = {
            "PaperAstraSuccesses",
            "PaperAstraCharge",
            "PaperAstraCostPerProof",
            "PaperAstraCalls",
            "PaperAstraAFiveCharge",
            "PaperAstraBSixCharge",
            "PaperFableSuccesses",
            "PaperFableCharge",
            "PaperFableCostPerProof",
            "PaperFableAFiveCharge",
            "PaperFableAFiveCalls",
            "PaperFableAFiveMinutes",
        }
        macros.update({key: value for key, value in additional_macros.items() if key in used})
    for path, digest in list(evidence.files.items()):
        evidence.bytes(Path(path), digest)
    lines = ["\\newcommand{\\" + name + "}{" + value + "}" for name, value in macros.items()]
    lines.extend(
        "\\newcommand{\\" + macro + "}{%\n" + tables[name] + "}"
        for name, macro in TABLE_MACROS.items()
        if name in tables
    )
    output.mkdir(parents=True)
    (output / "results.tex").write_text("\n".join(lines) + "\n")
    (output / "results.json").write_text(
        json.dumps(compact_results(model, evidence), indent=2) + "\n"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--combined", type=Path, required=True)
    parser.add_argument(
        "--annotations", type=Path, required=True, help="pooled-trace-statistics.json"
    )
    parser.add_argument("--annotation-provenance", type=Path, required=True)
    parser.add_argument(
        "--diagnostic", type=Path, help="Completed B6 Ax retry selected for both reported phases"
    )
    parser.add_argument("--diagnostic-supplement", type=Path)
    parser.add_argument("--diagnostic-supplement-sha256")
    parser.add_argument(
        "--replacement",
        type=Path,
        action="append",
        default=[],
        help="Repeat for each declared replacement",
    )
    parser.add_argument("--replacement-manifest-sha256", action="append", default=[])
    parser.add_argument("--replacement-annotation", type=Path)
    parser.add_argument("--replacement-annotation-sha256")
    parser.add_argument("--astra-experiment", type=Path)
    parser.add_argument("--fable-experiment", type=Path)
    parser.add_argument("--budget-policy", choices=POLICIES, default="last-dispatch")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    require(
        len(args.replacement) == len(args.replacement_manifest_sha256),
        "Replacement path and manifest hash must be supplied together",
    )
    require(
        not args.replacement_annotation or args.replacement,
        "Replacement annotation requires replacement input",
    )
    require(
        bool(args.replacement_annotation) == bool(args.replacement_annotation_sha256),
        "Replacement annotation path and hash must be supplied together",
    )
    runs, annotations, diagnostic, evidence = load_inputs(
        args.combined,
        args.annotations,
        args.annotation_provenance,
        args.diagnostic,
        args.diagnostic_supplement,
        args.diagnostic_supplement_sha256,
    )
    replacements = [
        load_replacement(path, digest, runs, evidence)
        for path, digest in zip(args.replacement, args.replacement_manifest_sha256, strict=True)
    ]
    replacement_annotation = None
    if args.replacement_annotation:
        b2 = next((row for row in replacements if row.run["run_id"] == REPLACEMENT_RUN_ID), None)
        require(b2 is not None, "B2 replacement annotation requires its selected run")
        replacement_annotation = load_replacement_annotation(
            args.replacement_annotation, b2, evidence, args.replacement_annotation_sha256
        )
    model = summarize(
        runs,
        annotations,
        policy=args.budget_policy,
        diagnostic=diagnostic,
        replacements=replacements,
        replacement_annotation=replacement_annotation,
    )
    add_campaigns(
        model,
        [
            load_campaign(directory, campaign, evidence, policy=args.budget_policy)
            for campaign in MINI_CAMPAIGN_MODELS
            if (directory := getattr(args, campaign + "_experiment")) is not None
        ],
    )
    write_tables(model, args.output, evidence)
    print(
        json.dumps(
            {
                "output": str(args.output),
                "budget_policy": args.budget_policy,
                "checkpoint_changes": model["checkpoint_changes"],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
