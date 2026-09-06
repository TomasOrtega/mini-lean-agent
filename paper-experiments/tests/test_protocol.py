from collections import Counter
from decimal import Decimal

import pytest

from paper_experiments.protocol import (
    GEMINI,
    OPUS,
    PROBLEMS,
    Limits,
    content_hash,
    register_runs,
    validate_registration,
)


def test_registration_preserves_pairs_and_predeclares_only_four_continuations():
    manifest = register_runs(1234)
    assert manifest == register_runs(1234)
    assert manifest != register_runs(1235)
    validate_registration(manifest)
    runs = manifest["runs"]
    assert len({r["run_id"] for r in runs}) == 36
    assert Counter(r["model"] for r in runs) == {GEMINI: 24, OPUS: 12}
    assert {r["replicate"] for r in runs} == {1}
    assert Counter(r["harness"] for r in runs if r["model"] == GEMINI) == {
        "mini": 12,
        "axprover": 12,
    }
    extended = [r for r in runs if r["extendable"]]
    assert {(r["harness"], r["problem"], r["replicate"]) for r in extended} == {
        (h, p, 1) for h in ("mini", "axprover") for p in ("A5", "B6")
    }
    for first, second, anchor in zip(runs[::3], runs[1::3], runs[2::3], strict=True):
        assert first["model"] == second["model"] == GEMINI
        assert (first["problem"], first["replicate"]) == (second["problem"], second["replicate"])
        assert first["harness"] != second["harness"]
        assert (anchor["model"], anchor["harness"]) == (OPUS, "mini")
    assert Counter(r["problem"] for r in runs) == dict.fromkeys(PROBLEMS, 3)
    assert {r["problem"] for r in runs[2::3]} == set(PROBLEMS)
    assert sum(
        Limits.for_run(
            r, "extended" if r["extendable"] else "low" if r["model"] == GEMINI else "anchor"
        ).dollars
        for r in runs
    ) == Decimal("300")
    assert sum(Limits.for_run(r, "low").dollars for r in runs if r["model"] == GEMINI) == 48
    assert manifest["nominal_allocation_usd"] == {
        "gemini_low": "48",
        "gemini": "240",
        "opus": "60",
        "total": "300",
    }
    assert manifest["api_call_limits"] == {
        problem: 300 if problem in {"A5", "B6"} else 200 for problem in PROBLEMS
    }


@pytest.mark.parametrize("problem", PROBLEMS)
def test_problem_call_limits_apply_to_every_panel_without_changing_dollars_or_time(problem):
    calls = 300 if problem in {"A5", "B6"} else 200
    matching = [run for run in register_runs(1)["runs"] if run["problem"] == problem]
    for run in matching:
        phase = "low" if run["model"] == GEMINI else "anchor"
        limits = Limits.for_run(run, phase)
        assert limits.calls == calls
        assert limits.dollars == (2 if phase == "low" else 5)
        assert limits.active_seconds == 7200
        if run["extendable"]:
            assert Limits.for_run(run, "extended") == Limits(Decimal("50"), 300, 86400)


def test_registration_rejects_previous_protocol_even_with_a_valid_hash():
    manifest = register_runs(42)
    manifest["schema_version"] = 1
    manifest["manifest_sha256"] = content_hash(
        {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    )
    with pytest.raises(ValueError, match="frozen protocol"):
        validate_registration(manifest)


def test_registration_rejects_cherry_picked_replicates():
    manifest = register_runs(42)
    manifest["runs"][0]["replicate"] = 7
    with pytest.raises(ValueError, match="frozen protocol"):
        validate_registration(manifest)


def test_registration_rejects_changed_call_caps_even_with_a_valid_hash():
    manifest = register_runs(42)
    manifest["api_call_limits"]["A5"] = 301
    manifest["manifest_sha256"] = content_hash(
        {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    )
    with pytest.raises(ValueError, match="frozen protocol"):
        validate_registration(manifest)


def test_selected_problem_registration_freezes_only_a1_and_its_budgets():
    manifest = register_runs(2025, problems=["A1"])
    validate_registration(manifest)
    assert manifest["scope"] == {
        "kind": "selected_problems",
        "problems": ["A1"],
        "benchmark_problem_count": 12,
    }
    assert len(manifest["runs"]) == 3
    assert {run["problem"] for run in manifest["runs"]} == {"A1"}
    assert [run["order"] for run in manifest["runs"]] == [0, 1, 2]
    assert not any(run["extendable"] for run in manifest["runs"])
    assert manifest["api_call_limits"] == {"A1": 200}
    assert manifest["nominal_allocation_usd"] == {
        "gemini_low": "4",
        "gemini": "4",
        "opus": "5",
        "total": "9",
    }


def test_selected_hard_problems_keep_cumulative_extension_allocations():
    manifest = register_runs(2, problems=["B6", "A1"])
    assert manifest == register_runs(2, problems=["A1", "B6"])
    assert len(manifest["runs"]) == 6
    assert sum(run["extendable"] for run in manifest["runs"]) == 2
    assert manifest["nominal_allocation_usd"] == {
        "gemini_low": "8",
        "gemini": "104",
        "opus": "10",
        "total": "114",
    }
    assert manifest["api_call_limits"] == {"A1": 200, "B6": 300}


@pytest.mark.parametrize("problems", [[], ["A7"], ["A1", "A1"]])
def test_registration_rejects_invalid_explicit_problem_scope(problems):
    with pytest.raises(ValueError, match="problem"):
        register_runs(2, problems=problems)


def test_original_registration_remains_byte_identical():
    assert (
        register_runs(2025)["manifest_sha256"]
        == "e42b7e716fb83cca147d216217ad3c09433bbb76b0ae94e4c8c664c6f3e6f084"
    )


def test_astra_registration_is_twelve_fresh_mini_attempts_with_frozen_limits():
    from paper_experiments.protocol import ASTRA

    manifest = register_runs(2025, campaign="astra")
    validate_registration(manifest)
    assert manifest == register_runs(2025, campaign="astra")
    assert manifest != register_runs(2026, campaign="astra")
    assert manifest["scope"]["problems"] == list(PROBLEMS)
    assert Counter(run["problem"] for run in manifest["runs"]) == dict.fromkeys(PROBLEMS, 1)
    assert {run["model"] for run in manifest["runs"]} == {ASTRA}
    assert {run["harness"] for run in manifest["runs"]} == {"mini"}
    assert {run["replicate"] for run in manifest["runs"]} == {1}
    assert not any(run["extendable"] for run in manifest["runs"])
    assert manifest["workers"] == 2
    assert manifest["checkpoints_usd"] == ["2", "5", "10", "20", "50"]
    assert manifest["nominal_allocation_usd"] == {"astra": "600", "total": "600"}
    for run in manifest["runs"]:
        assert Limits.for_run(run, "astra") == Limits(
            Decimal("50"), 300 if run["problem"] in {"A5", "B6"} else 200, 86400
        )
        assert manifest["limits"][run["problem"]] == Limits.for_run(run, "astra").to_dict()
        with pytest.raises(ValueError, match="Invalid phase"):
            Limits.for_run(run, "low")
    manifest["limits"]["A5"]["dollars"] = "100"
    manifest["manifest_sha256"] = content_hash(
        {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    )
    with pytest.raises(ValueError, match="frozen protocol"):
        validate_registration(manifest)


def test_astra_registration_cannot_exclude_a_pilot_problem():
    with pytest.raises(ValueError, match="all twelve"):
        register_runs(2025, [problem for problem in PROBLEMS if problem != "A5"], campaign="astra")


def test_astra_smoke_has_a_small_independent_allowance():
    from paper_experiments.protocol import ASTRA

    assert Limits.for_run(
        {"model": ASTRA, "harness": "mini", "problem": "smoke"}, "astra"
    ) == Limits(Decimal("2"), 6, 1800)


def test_astra_registration_remains_byte_identical_after_fable_support():
    assert (
        register_runs(2025, campaign="astra")["manifest_sha256"]
        == "b969dd51d57728bb501ce04928dabdff0fd173e3e8015f7b698913a9d6b1b5d6"
    )


def test_fable_registration_is_independent_and_uses_the_same_fresh_budget_protocol():
    from paper_experiments.protocol import FABLE

    assert FABLE == "claude-fable-5-1"
    manifest = register_runs(2025, campaign="fable")
    validate_registration(manifest)
    assert manifest["campaign"] == manifest["phase"] == "fable"
    assert manifest["workers"] == 2
    assert manifest["nominal_allocation_usd"] == {"fable": "600", "total": "600"}
    assert manifest["checkpoints_usd"] == ["2", "5", "10", "20", "50"]
    assert Counter(run["problem"] for run in manifest["runs"]) == dict.fromkeys(PROBLEMS, 1)
    assert {run["model"] for run in manifest["runs"]} == {FABLE}
    assert {run["harness"] for run in manifest["runs"]} == {"mini"}
    assert {run["replicate"] for run in manifest["runs"]} == {1}
    assert not any(run["extendable"] for run in manifest["runs"])
    astra_ids = {run["run_id"] for run in register_runs(2025, campaign="astra")["runs"]}
    assert not astra_ids.intersection(run["run_id"] for run in manifest["runs"])
    for run in manifest["runs"]:
        assert Limits.for_run(run, "fable") == Limits(
            Decimal("50"), 300 if run["problem"] in {"A5", "B6"} else 200, 86400
        )
        with pytest.raises(ValueError, match="Invalid phase"):
            Limits.for_run(run, "astra")
    assert Limits.for_run(
        {"model": FABLE, "harness": "mini", "problem": "smoke"}, "fable"
    ) == Limits(Decimal("2"), 6, 1800)
    with pytest.raises(ValueError, match="all twelve"):
        register_runs(2025, ["A1"], campaign="fable")
    manifest["runs"][0]["replicate"] = 2
    with pytest.raises(ValueError, match="frozen protocol"):
        validate_registration(manifest)
