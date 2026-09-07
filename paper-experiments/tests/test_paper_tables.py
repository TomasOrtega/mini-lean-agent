from copy import deepcopy
from decimal import Decimal
from pathlib import Path

import pytest

from paper_experiments import paper_tables as tables
from paper_experiments.protocol import GEMINI, register_runs


def state_fixture(run, *, charge="2.00150925", prior="1.99259625", verified=True):
    phase = "low" if run["model"] == GEMINI else "anchor"
    attempts = [
        {"attempt_id": 1, "charge_usd": prior, "completed": True},
        {"attempt_id": 2, "charge_usd": str(Decimal(charge) - Decimal(prior)), "completed": True},
    ]
    snapshot = {
        "index": 0,
        "phase": phase,
        "calls": 2,
        "charge_usd": charge,
        "active_seconds": 10,
        "verification_completed_seconds": 12,
        "verification": {"verified": verified},
    }
    endpoint = {
        "phase": phase,
        "snapshot_count": 1,
        "active_seconds": 12,
        "stop_reason": "agent_exit",
        "resumable": False,
        "verified": False,
        "ledger": {
            "calls": 2,
            "charge_usd": charge,
            "known_charge_usd": charge,
            "accounting_complete": True,
            "attempts": attempts,
            "tokens": {},
        },
    }
    return {
        "run": run,
        "paused": True,
        "phase": phase,
        "snapshots": [snapshot],
        "low_endpoint": endpoint if phase == "low" else None,
        "endpoint": endpoint,
    }


@pytest.fixture
def ax_run():
    return next(
        r
        for r in register_runs(2025)["runs"]
        if r["problem"] == "A1" and r["harness"] == "axprover"
    )


def test_crossing_call_can_be_reclassified_without_mutating_frozen_state(ax_run):
    state = state_fixture(ax_run)
    original = deepcopy(state)
    assert not tables.checkpoint(state, "2", "low", "strict")
    assert tables.checkpoint(state, "2", "low", "last-dispatch")
    assert state == original


@pytest.mark.parametrize(
    "phase,threshold,charge,prior",
    [
        ("low", "0.50", "0.518", "0.49"),
        ("low", "1", "1.105", "0.99"),
        ("anchor", "2", "2.01", "1.99"),
        ("extended", "5", "5.01", "4.99"),
    ],
)
def test_interior_checkpoints_remain_strict_even_when_last_dispatch_began_below(
    phase, threshold, charge, prior
):
    run = next(
        r
        for r in register_runs(0)["runs"]
        if (r["model"] != GEMINI if phase == "anchor" else r["extendable"])
    )
    state = state_fixture(run, charge=charge, prior=prior)
    assert not tables.checkpoint(state, threshold, phase, "last-dispatch")


@pytest.mark.parametrize("change", ["over_calls", "over_time", "unknown", "already_above"])
def test_last_dispatch_does_not_relax_other_safeguards(ax_run, change):
    state = state_fixture(ax_run)
    if change == "over_calls":
        state["snapshots"][0]["calls"] = 201
    elif change == "over_time":
        state["snapshots"][0]["verification_completed_seconds"] = 7201
    elif change == "unknown":
        state["endpoint"]["ledger"]["attempts"][0]["charge_usd"] = None
    else:
        state = state_fixture(ax_run, charge="2.02", prior="2.01")
    assert not tables.checkpoint(state, "2", "low", "last-dispatch")


def test_later_continuation_cannot_change_low_checkpoint():
    run = next(r for r in register_runs(0)["runs"] if r["extendable"])
    state = state_fixture(run, charge="1", prior="0.5", verified=False)
    later = {
        **state["snapshots"][0],
        "index": 1,
        "phase": "extended",
        "verification": {"verified": True},
    }
    state["snapshots"].append(later)
    state["endpoint"] = {**state["endpoint"], "phase": "extended", "snapshot_count": 2}
    state["phase"] = "extended"
    assert not tables.checkpoint(state, "2", "low", "last-dispatch")
    assert tables.checkpoint(state, "5", "extended", "last-dispatch")


def test_unavailable_charge_display_is_a_bound_not_a_fabricated_total():
    text = tables.money(None, "0.09796515")
    assert "geq" in text and "0.097965" in text
    assert "TBD" not in text
    assert tables.money(None, None) == r"\textsf{unavailable}"
    assert tables.money(None, "0.09796599").endswith("0.097965")


def test_regeneration_is_byte_identical_and_has_no_placeholder(tmp_path):
    model = tables.summarize(dataset(), {}, policy="strict")
    evidence = tables.Evidence()
    first, second = tmp_path / "first", tmp_path / "second"
    tables.write_tables(model, first, evidence)
    tables.write_tables(model, second, evidence)
    assert {p.name: p.read_bytes() for p in first.iterdir()} == {
        p.name: p.read_bytes() for p in second.iterdir()
    }
    assert all("TBD" not in p.read_text() for p in first.iterdir())
    with pytest.raises(ValueError, match="fresh"):
        tables.write_tables(model, first, evidence)


def test_changed_input_is_rejected_before_any_output_write(tmp_path):
    source = tmp_path / "source.json"
    source.write_text("{}")
    evidence = tables.Evidence()
    evidence.read(source)
    source.write_text('{"changed":true}')
    output = tmp_path / "tables"
    with pytest.raises(ValueError, match="Changed evidence"):
        tables.write_tables(tables.summarize(dataset(), {}, policy="strict"), output, evidence)
    assert not output.exists()


def test_opus_two_dollar_panel_never_implies_two_dollar_spend_ratio():
    data = dataset()
    row = next(r for r in data if r.run["model"] != GEMINI)
    row.state = state_fixture(row.run, charge="1", prior="0.5")
    row.initial_reconciled = row.final_reconciled = "5"
    model = tables.summarize(data, {}, policy="strict")
    for panel in model["panels"][2:]:
        assert panel["successes"] == 1
        assert panel["charge_per_success_usd"] is None
        assert panel["known_charge_per_success_usd"] is None
        assert panel["charge_basis"] == "full Opus $5 anchor"


def test_unfinished_frozen_run_is_rejected_before_ledger_read(tmp_path, monkeypatch, ax_run):
    state = state_fixture(ax_run)
    state["paused"] = False
    monkeypatch.setattr(tables, "_load_run", lambda *args: state)
    with pytest.raises(ValueError, match="Unfinished"):
        tables.load_frozen(ax_run, tmp_path, tables.Evidence())


@pytest.mark.parametrize("relative", [False, True])
def test_supplement_must_bind_provider_generation_and_preserve_primary(
    tmp_path, monkeypatch, ax_run, relative
):
    import json

    from paper_experiments.protocol import content_hash

    state = state_fixture(ax_run, charge="1", prior="0.5", verified=False)
    state["endpoint"]["stop_reason"] = "infrastructure_failure"
    ledger = state["endpoint"]["ledger"]
    ledger.update(charge_usd=None, known_charge_usd="0.5", accounting_complete=False)
    ledger["attempts"][1]["charge_usd"] = None
    ledger["attempts"][1]["response_metadata"] = {"response_id": "generation"}
    original = deepcopy(state)
    primaries = {}
    for name in ["controller.json", "ledger.jsonl", "low-endpoint.json"]:
        path = tmp_path / name
        path.write_text("{}")
        primaries[str(path)] = tables.hashlib.sha256(path.read_bytes()).hexdigest()
    generation = {"id": "generation", "is_byok": False, "total_cost": "0.2"}
    receipt = {
        "run_id": ax_run["run_id"],
        "primary_hashes": primaries,
        "summary": {
            "primary_accounting_complete": False,
            "primary_total_charge_usd": None,
            "unresolved_primary_attempts": [2],
            "registered_calls": 2,
            "primary_known_charge_usd": "0.5",
            "supplemental_accounting_complete": True,
            "reconciled_total_charge_usd": "0.7",
        },
        "resolutions": [
            {
                "attempt_id": 2,
                "response_id": "generation",
                "status": "reconciled",
                "receipt_charge_usd": "0.2",
                "generation": generation,
                "generation_sha256": content_hash(generation),
            }
        ],
    }
    path = tmp_path / "supplement.json"
    monkeypatch.chdir(tmp_path)
    directory = Path(".") if relative else tmp_path

    def invoke():
        path.write_text(json.dumps(receipt))
        row = {
            "run_id": ax_run["run_id"],
            "accounting_exception": {
                "endpoint_sha256": primaries[str(tmp_path / "low-endpoint.json")],
                "supplement_path": str(path),
                "supplement_sha256": tables.hashlib.sha256(path.read_bytes()).hexdigest(),
            },
        }
        return tables.supplemental_charge(row, state, directory, tables.Evidence())

    assert invoke() == "0.7"
    assert state == original
    receipt["resolutions"][0]["generation"]["total_cost"] = "0.3"
    with pytest.raises(ValueError, match="Provider receipt changed"):
        invoke()
    receipt["resolutions"][0]["generation_sha256"] = content_hash(generation)
    with pytest.raises(ValueError, match="Provider receipt charge differs"):
        invoke()


def dataset():
    data = []
    for run in register_runs(2025)["runs"]:
        state = state_fixture(run, charge="1", prior="0.5", verified=False)
        data.append(tables.RunData(run, state, "fixture", "1", "1"))
    return data


def test_b6_retry_is_selected_once_in_low_and_extended_panels():
    data = dataset()
    run = next(r.run for r in data if r.run["problem"] == "B6" and r.run["harness"] == "axprover")
    retry = tables.RunData(
        run, state_fixture(run, charge="8", prior="7", verified=True), "retry", "8", "8"
    )
    retry.state["endpoint"]["phase"] = "extended"
    retry.state["phase"] = "extended"
    model = tables.summarize(data, {}, policy="last-dispatch", diagnostic=retry)
    assert all(row["denominator"] == 12 for row in model["panels"])
    assert sum(row["successes"] for row in model["panels"]) == 0
    assert len(model["runs"]) == 36 and len(model["hard"]) == 4
    hard = next(row for row in model["hard"] if row["run_id"] == run["run_id"])
    assert hard["directory"] == "retry" and hard["verified"] is True
    assert model["costs"]["reported_model_charge_usd"] == "43"


def test_unknown_charge_and_zero_success_keep_nullable_ratios():
    data = dataset()
    row = next(
        r
        for r in data
        if r.run["problem"] == "B2" and r.run["harness"] == "mini" and r.run["model"] == GEMINI
    )
    row.state["endpoint"]["ledger"].update(charge_usd=None, accounting_complete=False)
    row.initial_reconciled = row.final_reconciled = None
    model = tables.summarize(data, {}, policy="strict")
    mini = model["panels"][0]
    assert mini["charge_usd"] is None
    assert mini["known_charge_usd"] == "12"
    assert mini["charge_per_success_usd"] is None
    assert mini["known_charge_per_success_usd"] is None


def test_exact_checkpoint_change_audit_identifies_a1_ax_only(ax_run):
    data = dataset()
    for row in data:
        if row.run == ax_run:
            row.state = state_fixture(ax_run)
    model = tables.summarize(data, {}, policy="last-dispatch")
    assert model["checkpoint_changes"] == [
        {
            "run_id": ax_run["run_id"],
            "phase": "low",
            "threshold_usd": "2",
            "strict": False,
            "selected_policy": True,
        }
    ]


def replacement_fixture(data, *, verified=False, charge="1.3"):
    original = next(
        r
        for r in data
        if r.run["problem"] == "B2" and r.run["harness"] == "mini" and r.run["model"] == GEMINI
    )
    state = state_fixture(original.run, charge=charge, prior="0.5", verified=verified)
    return tables.RunData(original.run, state, "replacement", charge, charge)


def test_b2_replacement_selects_once_and_omits_discarded_spend():
    data = dataset()
    original = next(r for r in data if r.run["run_id"] == tables.REPLACEMENT_RUN_ID)
    original.state["endpoint"]["ledger"].update(
        charge_usd=None, known_charge_usd="0.09796515", accounting_complete=False
    )
    original.initial_reconciled = original.final_reconciled = None
    archived = deepcopy(data)
    replacement = replacement_fixture(data, verified=True)
    model = tables.summarize(data, {}, policy="last-dispatch", replacements=[replacement])
    mini = model["panels"][0]
    assert mini["denominator"] == 12 and mini["successes"] == 1
    assert mini["charge_usd"] == "12.3"
    assert model["costs"]["reported_model_charge_usd"] == "36.3"
    b2 = [r for r in model["runs"] if r["run_id"] == original.run["run_id"]]
    assert len(b2) == 1 and b2[0]["directory"] == "replacement"
    assert data == archived


def test_replacement_failure_is_used_instead_of_best_of_two():
    data = dataset()
    original = next(r for r in data if r.run["run_id"] == tables.REPLACEMENT_RUN_ID)
    original.state["snapshots"][0]["verification"]["verified"] = True
    replacement = replacement_fixture(data, verified=False)
    model = tables.summarize(data, {}, policy="strict", replacements=[replacement])
    assert model["panels"][0]["successes"] == 0
    b2 = next(row for row in model["runs"] if row["run_id"] == tables.REPLACEMENT_RUN_ID)
    assert not b2["checkpoints"]["2"]
    assert original.state["snapshots"][0]["verification"]["verified"]


def test_replacement_must_match_its_registered_problem():
    data = dataset()
    replacement = replacement_fixture(data)
    replacement.run = {**replacement.run, "problem": "A2"}
    with pytest.raises(ValueError, match="differs from the registered run"):
        tables.summarize(data, {}, policy="strict", replacements=[replacement])


def test_original_b2_annotation_is_not_reused_for_replacement():
    data = dataset()
    annotations = {
        r.run["run_id"]: {
            "mathlib_search": False,
            "scratch_files": False,
            "lean_invocations": 0,
            "first_lean_check_failed": False,
            "failure_category": "proof",
        }
        for r in data
        if r.run["model"] == GEMINI and r.run["harness"] == "mini"
    }
    replacement = replacement_fixture(data, verified=True)
    model = tables.summarize(data, annotations, policy="strict", replacements=[replacement])
    assert model["trace"]["annotated_low_mini_runs"] == 11
    assert model["trace"]["status"] == "incomplete annotations"
    annotation = {
        **annotations[replacement.run["run_id"]],
        "failure_category": "none",
        "lean_invocations": 8,
    }
    complete = tables.summarize(
        data,
        annotations,
        policy="strict",
        replacements=[replacement],
        replacement_annotation=annotation,
    )
    assert complete["trace"]["annotated_low_mini_runs"] == 12
    assert complete["trace"]["median_lean_invocations_among_annotated_successes"] == 8


def test_selected_retries_replace_old_costs_without_adding_attempts():
    data = dataset()
    replacement = replacement_fixture(data)
    run = next(r.run for r in data if r.run["problem"] == "B6" and r.run["harness"] == "axprover")
    diagnostic = tables.RunData(
        run, state_fixture(run, charge="8", prior="7", verified=False), "diagnostic", "8", "8"
    )
    model = tables.summarize(
        data, {}, policy="strict", replacements=[replacement], diagnostic=diagnostic
    )
    assert len(model["runs"]) == 36 and len(model["hard"]) == 4
    assert model["costs"]["reported_model_charge_usd"] == "43.3"
    assert len({row["run_id"] for row in model["runs"]}) == 36


def test_replacement_requires_declaration_before_execution():
    original = next(r for r in dataset() if r.run["run_id"] == tables.REPLACEMENT_RUN_ID)
    manifest = {
        "kind": "single_run_replacement_not_original_registration",
        "run": original.run,
        "selection_policy": "replace B2-mini regardless of outcome",
        "phase": "low",
        "original_outcome_retained": True,
        "declared_at": "2026-09-05T12:00:00+00:00",
        "limits": {"low": {"dollars": "2", "calls": 200, "active_seconds": 7200}},
    }
    tables.validate_replacement_manifest(manifest, original, "2026-09-05T12:00:01+00:00")
    with pytest.raises(ValueError, match="declared after"):
        tables.validate_replacement_manifest(manifest, original, "2026-09-05T11:59:59+00:00")
    manifest["selection_policy"] = "choose the successful attempt"
    with pytest.raises(ValueError, match="independent of outcome"):
        tables.validate_replacement_manifest(manifest, original)


def test_cli_rejects_unbound_replacement_before_reading_inputs(monkeypatch):
    import sys

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "make_tables",
            "--combined",
            "/unused",
            "--annotations",
            "/unused",
            "--annotation-provenance",
            "/unused",
            "--output",
            "/unused",
            "--replacement",
            "/unbound",
        ],
    )
    monkeypatch.setattr(
        tables, "load_inputs", lambda *args: pytest.fail("unbound replacement reached loader")
    )
    with pytest.raises(ValueError, match="supplied together"):
        tables.main()


def test_replacement_table_uses_selected_rows_only(tmp_path):
    import json

    data = dataset()
    replacement = replacement_fixture(data, verified=True)
    original = next(r for r in data if r.run["run_id"] == tables.REPLACEMENT_RUN_ID)
    original.state["snapshots"][0]["verification"]["verified"] = True
    model = tables.summarize(data, {}, policy="strict", replacements=[replacement])
    output = tmp_path / "tables"
    tables.write_tables(model, output, tables.Evidence())
    text = (output / "results.tex").read_text()
    assert "2/12" not in text and "1/12" in text
    assert "Excluded billing-error run" not in text
    assert r"Total & \$36.300000" in text
    assert "smoke" not in text.lower() and "operating costs" not in text
    result = json.loads((output / "results.json").read_text())
    b2 = [row for row in result["runs"] if row["run_id"] == tables.REPLACEMENT_RUN_ID]
    assert len(b2) == 1 and b2[0]["initial_reconciled_charge_usd"] == "1.3"


def test_manual_note_must_bind_replacement_endpoint_and_trace(tmp_path):
    import json

    replacement = replacement_fixture(dataset())
    replacement.directory = str(tmp_path / "replacement")
    row = {
        "run_id": tables.REPLACEMENT_RUN_ID,
        "mathlib_search": False,
        "scratch_files": False,
        "lean_invocations": 0,
        "first_lean_check_failed": None,
        "failure_category": "proof",
        "evidence": "Reviewed complete trace.",
    }
    records = {}
    for root_name in ["original", "replacement"]:
        root = tmp_path / root_name
        trace = root / "snapshots" / "000000" / "driver-state.json"
        trace.parent.mkdir(parents=True)
        trace.write_text("{}")
        endpoint = root / "low-endpoint.json"
        endpoint.write_text("{}")
        records[root_name] = [
            {"path": str(p), "sha256": tables.hashlib.sha256(p.read_bytes()).hexdigest()}
            for p in [endpoint, trace]
        ]
    path = tmp_path / "note.json"
    path.write_text(json.dumps({"row": row, "evidence": records["original"]}))
    with pytest.raises(ValueError, match="own endpoint"):
        tables.load_replacement_annotation(path, replacement, tables.Evidence())
    path.write_text(json.dumps({"row": row, "evidence": records["replacement"]}))
    assert tables.load_replacement_annotation(path, replacement, tables.Evidence()) == row
    with pytest.raises(ValueError, match="Changed evidence"):
        tables.load_replacement_annotation(path, replacement, tables.Evidence(), "f" * 64)


def test_cli_requires_annotation_hash_before_loading_replacement(monkeypatch):
    import sys

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "make_tables",
            "--combined",
            "/unused",
            "--annotations",
            "/unused",
            "--annotation-provenance",
            "/unused",
            "--output",
            "/unused",
            "--replacement",
            "/unused",
            "--replacement-manifest-sha256",
            "f" * 64,
            "--replacement-annotation",
            "/unbound-note",
        ],
    )
    monkeypatch.setattr(
        tables, "load_inputs", lambda *args: pytest.fail("unbound note reached loader")
    )
    with pytest.raises(ValueError, match="annotation path and hash"):
        tables.main()


def campaign_fixture(campaign):
    runs = []
    for run in register_runs(2025, campaign=campaign)["runs"]:
        state = state_fixture(run, charge="3", prior="1", verified=run["problem"] != "A5")
        state["phase"] = state["endpoint"]["phase"] = campaign
        state["snapshots"][0]["phase"] = campaign
        state["started_at"] = "2026-09-05T20:00:00+00:00"
        state["endpoint"]["frozen_at"] = "2026-09-05T20:00:12+00:00"
        for attempt in state["endpoint"]["ledger"]["attempts"]:
            attempt["charge_basis"] = "provider"
            attempt["response_metadata"] = {
                "billing": {
                    "is_byok": True,
                    "openrouter_charge_usd": "0",
                    "upstream_inference_cost_usd": attempt["charge_usd"],
                    "total_charge_usd": attempt["charge_usd"],
                },
                "usage": {
                    "cost": 0,
                    "is_byok": True,
                    "cost_details": {"upstream_inference_cost": attempt["charge_usd"]},
                },
            }
        runs.append(tables.RunData(run, state, f"{campaign}/{run['problem']}", "3", "3"))
    scheduler = [
        {
            "started_at": "2026-09-05T20:00:00+00:00",
            "finished_at": "2026-09-05T20:30:00+00:00",
            "results": [{"run_id": row.run["run_id"], "error": None} for row in runs],
        }
    ]
    smoke = {"ok": True, "cases": [{"ledger": deepcopy(runs[0].final["ledger"])}]}
    return runs, scheduler, smoke


@pytest.mark.parametrize("campaign", ["astra", "fable"])
def test_campaign_summary_counts_all_twelve_once_and_uses_byok_spending(campaign):
    data, scheduler, smoke = campaign_fixture(campaign)
    original = deepcopy(data)
    panel = tables.summarize_campaign(campaign, data, scheduler, smoke, policy="last-dispatch")
    assert panel["successes"] == 11
    assert panel["charge_usd"] == "36"
    assert panel["byok_upstream_charge_usd"] == "36"
    assert panel["openrouter_charge_usd"] == "0"
    assert panel["calls"] == 24 and panel["byok_calls"] == 24
    assert panel["wall_seconds"] == 1800
    assert panel["checkpoints"] == {"2": 0, "5": 11, "10": 11, "20": 11, "50": 11}
    assert panel["smoke_charge_usd"] == "3"
    assert data == original


def test_campaign_rejects_missing_problem_or_incomplete_scheduler():
    data, scheduler, smoke = campaign_fixture("astra")
    with pytest.raises(ValueError, match="twelve"):
        tables.summarize_campaign("astra", data[:-1], scheduler, smoke, policy="strict")
    scheduler[0].pop("finished_at")
    with pytest.raises(ValueError, match="scheduler"):
        tables.summarize_campaign("astra", data, scheduler, smoke, policy="strict")


def test_campaign_detects_omitted_upstream_byok_charge():
    data, scheduler, smoke = campaign_fixture("astra")
    ledger = data[0].final["ledger"]
    ledger["attempts"][0]["charge_usd"] = "0"
    with pytest.raises(ValueError, match="billing"):
        tables.summarize_campaign("astra", data, scheduler, smoke, policy="strict")


def test_campaign_cost_overrun_never_turns_an_unverified_candidate_into_success():
    data, scheduler, smoke = campaign_fixture("fable")
    row = next(row for row in data if row.run["problem"] == "A5")
    state = row.state
    state["snapshots"][0]["charge_usd"] = "50.10462"
    ledger = state["endpoint"]["ledger"]
    ledger.update(charge_usd="50.10462", known_charge_usd="50.10462")
    ledger["attempts"][1]["charge_usd"] = "49.10462"
    metadata = ledger["attempts"][1]["response_metadata"]
    metadata["billing"].update(upstream_inference_cost_usd="49.10462", total_charge_usd="49.10462")
    metadata["usage"]["cost_details"]["upstream_inference_cost"] = "49.10462"
    panel = tables.summarize_campaign("fable", data, scheduler, smoke, policy="last-dispatch")
    assert panel["successes"] == 11
    assert next(row for row in panel["runs"] if row["problem"] == "A5")["verified"] is False
    assert panel["charge_usd"] == "83.10462"


def test_modern_tables_keep_selected_results_and_count_only_reported_model_spending(tmp_path):
    import json

    original = tables.summarize(dataset(), {}, policy="strict")
    model = deepcopy(original)
    panels = [
        tables.summarize_campaign(name, *campaign_fixture(name), policy="strict")
        for name in ("astra", "fable")
    ]
    tables.add_campaigns(model, panels)
    output = tmp_path / "modern"
    tables.write_tables(model, output, tables.Evidence())
    assert model["panels"] == original["panels"] and model["runs"] == original["runs"]
    assert model["costs"]["reported_model_charge_usd"] == "108"
    assert {p.name for p in output.iterdir()} == {"results.tex", "results.json"}
    macros = (output / "results.tex").read_text()
    assert r"\newcommand{\PaperAstraCharge}{\$36.00}" in macros
    assert r"\newcommand{\PaperFableAFiveCalls}{2}" in macros
    assert all(
        name + "}{%" in macros
        for key, name in tables.TABLE_MACROS.items()
        if not key.startswith("rq1-")
    )
    assert macros.count(r"\begin{tabular}") == 7
    result = json.loads((output / "results.json").read_text())
    assert len(result["campaigns"]) == 2
    assert "paired_effect" not in result
    assert "smoke_charge_usd" not in (output / "results.json").read_text()
    assert len(result["reproducibility"]["registrations"]) == 0


def test_selected_b6_retry_replaces_zero_dispatch_without_mutating_archive():
    data = dataset()
    original = next(
        row for row in data if row.run["problem"] == "B6" and row.run["harness"] == "axprover"
    )
    original.state = state_fixture(original.run, charge="0", prior="0", verified=False)
    original.initial_reconciled = original.final_reconciled = "0"
    retry = tables.RunData(
        original.run,
        state_fixture(original.run, charge="2", prior="1", verified=False),
        "selected-b6-retry",
        "2",
        "8",
    )
    retry.state["endpoint"] = deepcopy(retry.state["low_endpoint"])
    retry.state["endpoint"]["phase"] = "extended"
    retry.state["endpoint"]["ledger"].update(charge_usd="8", known_charge_usd="8")
    archived = deepcopy(data)
    model = tables.summarize(data, {}, policy="last-dispatch", diagnostic=retry)
    selected = [row for row in model["runs"] if row["run_id"] == original.run["run_id"]]
    assert len(selected) == 1
    assert selected[0]["directory"] == "selected-b6-retry"
    assert len(model["runs"]) == 36 and len(model["hard"]) == 4
    assert model["panels"][1]["charge_usd"] == "13"
    assert model["costs"]["reported_model_charge_usd"] == "43"
    assert data == archived


def test_export_contains_only_two_compact_result_files(tmp_path):
    import json

    model = tables.summarize(dataset(), {}, policy="strict")
    output = tmp_path / "results"
    tables.write_tables(model, output, tables.Evidence())
    assert {p.name for p in output.iterdir()} == {"results.tex", "results.json"}
    result = json.loads((output / "results.json").read_text())
    assert "reproducibility" in result
    assert "input_files" not in result["reproducibility"]
    assert "output_files" not in result["reproducibility"]


@pytest.mark.parametrize("verified", [False, True])
def test_b2_and_b3_replacements_select_each_attempt_once(verified):
    data = dataset()
    b2 = replacement_fixture(data, verified=True)
    original = next(r for r in data if r.run["problem"] == "B3" and r.run["harness"] == "axprover")
    original.state = state_fixture(original.run, charge="0", prior="0", verified=not verified)
    original.initial_reconciled = original.final_reconciled = "0"
    b3 = tables.RunData(
        original.run,
        state_fixture(original.run, charge="1.7", prior="0.5", verified=verified),
        "b3-replacement",
        "1.7",
        "1.7",
    )
    archived = deepcopy(data)
    result = tables.summarize(data, {}, policy="last-dispatch", replacements=[b2, b3])
    assert len(result["runs"]) == 36
    assert all(panel["denominator"] == 12 for panel in result["panels"])
    assert result["panels"][0]["successes"] == 1
    assert result["panels"][1]["successes"] == int(verified)
    assert result["panels"][1]["charge_usd"] == "12.7"
    assert result["costs"]["reported_model_charge_usd"] == "37.0"
    selected = [r for r in result["runs"] if r["run_id"] == original.run["run_id"]]
    assert len(selected) == 1 and selected[0]["directory"] == "b3-replacement"
    assert data == archived
    with pytest.raises(ValueError, match="Duplicate replacement"):
        tables.summarize(data, {}, policy="strict", replacements=[b3, b3])


def test_b3_replacement_requires_its_original_settings_and_prior_declaration():
    original = next(
        r for r in dataset() if r.run["problem"] == "B3" and r.run["harness"] == "axprover"
    )
    manifest = {
        "kind": "single_run_replacement_not_original_registration",
        "run": original.run,
        "selection_policy": "replace B3-axprover regardless of outcome",
        "phase": "low",
        "original_outcome_retained": True,
        "declared_at": "2026-09-06T04:00:00+00:00",
        "limits": {"low": {"dollars": "2", "calls": 200, "active_seconds": 7200}},
    }
    tables.validate_replacement_manifest(manifest, original, "2026-09-06T04:00:01+00:00")
    with pytest.raises(ValueError, match="declared after"):
        tables.validate_replacement_manifest(manifest, original, "2026-09-06T03:59:59+00:00")
    manifest["limits"]["low"]["dollars"] = "5"
    with pytest.raises(ValueError, match="limits differ"):
        tables.validate_replacement_manifest(manifest, original)


def test_behavior_tables_keep_counts_and_excerpts_in_the_same_two_artifacts(tmp_path):
    import json

    counts = {
        "bash_requests": 50,
        "cat_and_lean_requests": 20,
        "program_requests": dict.fromkeys(
            ("text_search", "cat", "sed", "python3", "lean_check", "file_discovery"), 10
        ),
    }
    panel = {
        "calls": 60,
        "charge_usd": "1.20",
        "tokens": {
            "input": 100,
            "cache_read": 500,
            "cache_write": 0,
            "output": 10,
            "reasoning": 50,
        },
        "roles": {"memory_processor": {"charge_usd": "0.12"}},
        "runs": [
            {"stop_reason": "cost", "verified": False, "final_snapshot_rejected_for_sorry": True},
            {
                "stop_reason": "infrastructure_failure",
                "verified": False,
                "final_snapshot_rejected_for_sorry": True,
            },
        ],
    }
    model = tables.summarize(dataset(), {}, policy="strict")
    model["behavior"] = {
        "mini_shell": {"problems": [{"problem": "A1", **counts}], "totals": counts},
        "harnesses": {"mini": panel, "axprover": panel},
        "examples": [{"run_id": "example", "source_sha256": "a" * 64}],
    }
    output = tmp_path / "behavior"
    tables.write_tables(model, output, tables.Evidence())
    assert {p.name for p in output.iterdir()} == {"results.tex", "results.json"}
    result = json.loads((output / "results.json").read_text())
    assert result["behavior"] == model["behavior"]
    macros = (output / "results.tex").read_text()
    assert r"\newcommand{\PaperToolUseTable}{%" in macros
    assert r"\newcommand{\PaperUsageTable}{%" in macros
    assert r"\newcommand{\PaperMiniCachedInputPercent}{83.3\%}" in macros
    assert r"\newcommand{\PaperAxAuxiliarySpendPercent}{10.0\%}" in macros
    assert r"\newcommand{\PaperAxBudgetIncomplete}{1}" in macros


def test_paper_table_style_and_binary_verification_symbols(tmp_path):
    model = tables.summarize(dataset(), {}, policy="strict")
    tables.add_campaigns(
        model,
        [
            tables.summarize_campaign(name, *campaign_fixture(name), policy="strict")
            for name in ("astra", "fable")
        ],
    )
    output = tmp_path / "styled"
    tables.write_tables(model, output, tables.Evidence())
    text = (output / "results.tex").read_text()
    assert r"Harness & \$0.50 & \$1.00 & \$2.00 & Charge" in text
    assert r"mini-Lean-agent & 0/12 & 0/12 & 0/12 & \$12.00 \\" in text
    assert "Problem & mini Verified & mini Charge & Ax Verified & Ax Charge" in text
    assert "Problem & Harness & Verified & Charge & Calls & Hours" in text
    assert r"\begin{tabular}{@{}lcrcc@{}}" in text
    assert "Model & Verified & Charge & Calls & Minutes" in text
    assert "Charge/proof" not in text
    assert (
        "Problem & Verified & Charge & Calls & Minutes & Verified & Charge & Calls & Minutes"
        in text
    )
    assert r"Gemini 3.8 Flash & \$12.000000" in text
    assert r"Gemini 3.8 Flash (AxProverBase) & \$12.000000" in text
    assert "\\midrule\nTotal & \\$108.000000" in text
    assert r"\newcommand{\PaperVerified}{\textcolor{green!50!black}{\ding{51}}}" in text
    assert r"\newcommand{\PaperUnverified}{\textcolor{red!75!black}{\ding{55}}}" in text
    assert r"A1 & \PaperUnverified{} & \$1.000000" in text
    assert r"A1 & \PaperVerified{} & \$3.00" in text
    assert r"A5 & \PaperUnverified{} & \$3.00" in text
    assert " & yes & " not in text and " & no & " not in text
    assert model["campaigns"][0]["successes"] == 11
