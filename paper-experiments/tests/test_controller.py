import json
from decimal import Decimal

import pytest

from paper_experiments.controller import RunController, RunStopped
from paper_experiments.ledger import Ledger, PriceTable
from paper_experiments.protocol import GEMINI, register_runs


class Clock:
    time = 0.0

    def __call__(self):
        return self.time


def setup(tmp_path, verifier=None):
    run = next(
        r
        for r in register_runs(1)["runs"]
        if r["model"] == GEMINI
        and r["harness"] == "mini"
        and r["problem"] == "A5"
        and r["replicate"] == 1
    )
    prices = PriceTable(
        "2026-09-04",
        "fixture",
        {GEMINI: dict.fromkeys(("input", "output", "reasoning", "cache_read", "cache_write"), "1")},
    )
    ledger = Ledger(tmp_path / "ledger.jsonl", prices)
    clock = Clock()
    controller = RunController(
        run,
        tmp_path / "run",
        ledger,
        verifier or (lambda p: {"verified": p.read_text() == "valid"}),
        clock=clock,
    )
    candidate = tmp_path / "candidate.lean"
    candidate.parent.mkdir(parents=True, exist_ok=True)
    candidate.write_text("invalid")
    state = {
        "messages": [{"role": "user", "content": "original task"}],
        "tool_state": {"cwd": "/workspace"},
        "config_hash": "frozen",
    }
    return controller, ledger, clock, candidate, state


def request(controller, ledger, amount):
    controller.before_request()
    attempt = ledger.begin_attempt("proposer", GEMINI)
    ledger.finish_attempt(attempt, None, actual_charge=amount)


def test_cap_crossing_response_is_fully_charged_but_not_credited_earlier(tmp_path):
    controller, ledger, _, candidate, state = setup(tmp_path)
    request(controller, ledger, "0.4")
    controller.after_boundary(candidate, state)
    request(controller, ledger, "1.8")
    candidate.write_text("valid")
    controller.after_boundary(candidate, state)
    assert ledger.charge == Decimal("2.2")
    assert controller.state["low_endpoint"]["stop_reason"] == "cost"
    assert controller.checkpoints() == {"0.50": False, "1": False, "2": False}
    with pytest.raises(RunStopped):
        controller.before_request()
    original_low = json.loads((controller.output_dir / "low-endpoint.json").read_text())
    assert controller.resume_extended() == state
    assert controller.state["endpoint"]["verified"]
    assert controller.checkpoint("5")
    assert not controller.checkpoint("2", panel="low")
    assert json.loads((controller.output_dir / "low-endpoint.json").read_text()) == original_low


def test_future_proof_below_two_dollars_cannot_contaminate_time_limited_low(tmp_path):
    controller, ledger, clock, candidate, state = setup(tmp_path)
    request(controller, ledger, "0.001")
    clock.time = 7200
    controller.after_boundary(candidate, state)
    frozen = controller.state["low_endpoint"].copy()
    assert frozen["stop_reason"] == "elapsed_time"
    clock.time += 500_000
    reloaded = RunController(
        controller.state["run"], controller.output_dir, ledger, controller.evaluator, clock=clock
    )
    assert reloaded.active_seconds == 7200
    assert reloaded.resume_extended() == state
    request(reloaded, ledger, "0.1")
    candidate.write_text("valid")
    reloaded.after_boundary(candidate, state)
    assert reloaded.ledger.calls == 2
    assert reloaded.checkpoint("5")
    assert not reloaded.checkpoint("2", panel="low")
    assert reloaded.state["low_endpoint"] == frozen
    assert reloaded.state["driver_state"]["messages"] == state["messages"]


def test_hard_problem_call_limit_is_cumulative_across_continuation(tmp_path):
    controller, ledger, _, candidate, state = setup(tmp_path)
    for _ in range(300):
        request(controller, ledger, "0.001")
    controller.after_boundary(candidate, state)
    frozen = controller.state["low_endpoint"].copy()
    assert frozen["stop_reason"] == "call_count"
    assert frozen["limits"]["calls"] == 300
    assert controller.resume_extended() == state
    with pytest.raises(RunStopped, match="call_count"):
        request(controller, ledger, "0.001")
    assert ledger.calls == 300
    assert controller.state["endpoint"]["limits"]["calls"] == 300
    assert controller.state["low_endpoint"] == frozen


def test_time_is_cumulative_and_verification_latency_counts(tmp_path):
    controller, ledger, clock, candidate, state = setup(tmp_path)
    request(controller, ledger, "0.1")
    clock.time = 7199

    def slow_verifier(path):
        clock.time += 2
        return {"verified": True}

    controller.evaluator = slow_verifier
    controller.after_boundary(candidate, state)
    assert controller.state["low_endpoint"]["stop_reason"] == "elapsed_time"
    assert not controller.checkpoint("2")
    controller, ledger, clock, candidate, state = setup(tmp_path / "cumulative")
    request(controller, ledger, "0.1")
    clock.time = 7201
    controller.after_boundary(candidate, state)
    controller.resume_extended()
    clock.time += 79200
    with pytest.raises(RunStopped, match="elapsed_time"):
        controller.before_request()
    assert controller.state["endpoint"]["active_seconds"] == 86401


def test_immutable_snapshot_preserves_candidate_and_usage_before_later_edits(tmp_path):
    controller, ledger, _, candidate, state = setup(tmp_path)
    request(controller, ledger, "0.1")
    snapshot = controller.after_boundary(candidate, state)
    candidate.write_text("valid")
    state["messages"].append({"role": "assistant", "content": "future"})
    assert controller.state["driver_state"]["messages"] != state["messages"]
    assert not controller.checkpoint("0.5")
    assert snapshot["verification"]["verified"] is False


def test_unknown_billing_stops_without_treating_missing_usage_as_free(tmp_path):
    controller, ledger, _, candidate, state = setup(tmp_path)
    controller.before_request()
    attempt = ledger.begin_attempt("proposer", GEMINI)
    ledger.finish_attempt(attempt, None, error="timeout")
    controller.after_boundary(candidate, state)
    assert controller.state["endpoint"]["stop_reason"] == "infrastructure_failure"
    assert controller.state["endpoint"]["ledger"]["charge_usd"] is None
    with pytest.raises(ValueError, match="technically resumable"):
        controller.resume_extended()


class SimulatedCrash(BaseException):
    pass


def test_crash_during_verification_recovers_immutable_boundary_without_inference(tmp_path):
    controller, ledger, clock, candidate, state = setup(tmp_path)
    request(controller, ledger, "0.1")
    candidate.write_text("valid")

    def interrupted_verifier(path):
        raise SimulatedCrash()

    controller.evaluator = interrupted_verifier
    with pytest.raises(SimulatedCrash):
        controller.after_boundary(candidate, state)
    candidate.write_text("different future bytes")
    restored = RunController(
        controller.state["run"],
        controller.output_dir,
        ledger,
        lambda p: {"verified": p.read_text() == "valid"},
        clock=clock,
    )
    assert restored.state["endpoint"]["verified"]
    assert restored.state["driver_state"] == state
    assert len(restored.state["snapshots"]) == 1
    assert ledger.calls == 1


def test_missing_target_is_a_failed_artifact_that_can_be_repaired(tmp_path):
    controller, ledger, _, candidate, state = setup(tmp_path)
    request(controller, ledger, "0.1")
    candidate.unlink()
    snapshot = controller.after_boundary(candidate, state)
    assert snapshot["candidate_missing"]
    assert not snapshot["verification"]["verified"]
    assert not controller.state["paused"]
    request(controller, ledger, "0.1")
    candidate.write_text("valid")
    controller.after_boundary(candidate, state)
    assert controller.state["endpoint"]["verified"]


def test_terminal_harness_state_cannot_resume_when_a_budget_also_binds(tmp_path):
    controller, ledger, _, candidate, state = setup(tmp_path)
    request(controller, ledger, "2")
    state["resumable"] = False
    controller.after_boundary(candidate, state, stop_reason="agent_exit")
    assert controller.state["low_endpoint"]["stop_reason"] == "cost"
    assert not controller.state["low_endpoint"]["resumable"]
    with pytest.raises(ValueError, match="technically resumable"):
        controller.resume_extended()


@pytest.mark.parametrize("tamper", ["truncate", "change_settled", "change_pending"])
def test_active_resume_rejects_a_lost_or_changed_ledger_prefix(tmp_path, tamper):
    controller, ledger, clock, candidate, state = setup(tmp_path)
    request(controller, ledger, "0.3")
    controller.after_boundary(candidate, state)
    if tamper == "change_pending":
        ledger.begin_attempt("memory", GEMINI, {"request_sha256": "original"})
        controller._persist()
    events = [json.loads(line) for line in ledger.path.read_text().splitlines()]
    if tamper == "truncate":
        events = events[:1]
    elif tamper == "change_settled":
        events[2]["charge_usd"] = "0.4"
    else:
        events[-1]["metadata"]["request_sha256"] = "changed"
    ledger.path.write_text("\n".join(json.dumps(event) for event in events) + "\n")
    with pytest.raises(ValueError, match="ledger"):
        RunController(
            controller.state["run"],
            controller.output_dir,
            ledger,
            controller.evaluator,
            clock=clock,
        )


def test_active_resume_allows_settling_the_same_persisted_pending_attempt(tmp_path):
    controller, ledger, clock, candidate, state = setup(tmp_path)
    request(controller, ledger, "0.3")
    controller.after_boundary(candidate, state)
    attempt = ledger.begin_attempt("memory", GEMINI, {"request_sha256": "original"})
    controller._persist()
    ledger.finish_attempt(attempt, None, actual_charge="0.2")
    restored = RunController(
        controller.state["run"], controller.output_dir, ledger, controller.evaluator, clock=clock
    )
    assert restored.ledger.calls == 2
    assert restored.ledger.charge == Decimal("0.5")


def test_evaluator_cleanup_failure_stops_without_converting_it_to_proof_failure(tmp_path):
    from paper_experiments.resources import ContainerCleanupError

    controller, ledger, _, candidate, state = setup(tmp_path)
    request(controller, ledger, "0.1")

    def leaked_evaluator(path):
        raise ContainerCleanupError("evaluator container still alive")

    controller.evaluator = leaked_evaluator
    with pytest.raises(ContainerCleanupError, match="still alive"):
        controller.after_boundary(candidate, state)
    assert controller.state["endpoint"] is None
    assert controller.state["snapshots"][0]["verification"]["pending"]


@pytest.mark.parametrize("campaign", ["astra", "fable"])
def test_mini_campaign_continues_past_low_budget_and_freezes_its_own_cap(tmp_path, campaign):
    from paper_experiments.protocol import MINI_CAMPAIGN_MODELS

    model = MINI_CAMPAIGN_MODELS[campaign]

    run = register_runs(2025, campaign=campaign)["runs"][0]
    prices = PriceTable(
        "2026-09-05",
        "fixture",
        {model: dict.fromkeys(("input", "output", "reasoning", "cache_read", "cache_write"), "1")},
    )
    ledger = Ledger(tmp_path / "ledger.jsonl", prices)
    clock = Clock()
    controller = RunController(
        run,
        tmp_path / "run",
        ledger,
        lambda path: {"verified": path.read_text() == "valid"},
        clock=clock,
    )
    candidate = tmp_path / "candidate.lean"
    candidate.write_text("invalid")
    state = {"messages": [], "resumable": True}
    controller.before_request()
    attempt = ledger.begin_attempt("proposer", model)
    ledger.finish_attempt(attempt, None, actual_charge="20")
    clock.time = 7201
    controller.after_boundary(candidate, state)
    assert controller.phase == campaign
    assert not controller.state["paused"]
    assert controller.state["low_endpoint"] is None
    controller.before_request()
    attempt = ledger.begin_attempt("proposer", model)
    ledger.finish_attempt(attempt, None, actual_charge="30")
    candidate.write_text("valid")
    controller.after_boundary(candidate, state)
    assert controller.state["paused"]
    assert controller.state["endpoint"]["stop_reason"] == "verified"
    assert controller.checkpoints() == {
        "2": False,
        "5": False,
        "10": False,
        "20": False,
        "50": True,
    }
    assert (controller.output_dir / f"{campaign}-endpoint.json").exists()
    with pytest.raises(RunStopped):
        controller.before_request()
    restored = RunController(run, controller.output_dir, ledger, controller.evaluator, clock=clock)
    assert restored.state["endpoint"] == controller.state["endpoint"]
    assert restored.state["low_endpoint"] is None


@pytest.mark.parametrize("campaign", ["astra", "fable"])
def test_mini_campaign_smoke_checkpoint_respects_its_smaller_cap(tmp_path, campaign):
    from paper_experiments.protocol import MINI_CAMPAIGN_MODELS

    model = MINI_CAMPAIGN_MODELS[campaign]

    run = {"model": model, "harness": "mini", "problem": "smoke", "extendable": False}
    prices = PriceTable(
        "2026-09-05",
        "fixture",
        {model: dict.fromkeys(("input", "output", "reasoning", "cache_read", "cache_write"), "1")},
    )
    ledger = Ledger(tmp_path / "ledger.jsonl", prices)
    controller = RunController(run, tmp_path / "run", ledger, lambda path: {"verified": True})
    candidate = tmp_path / "candidate.lean"
    candidate.write_text("valid")
    controller.after_boundary(candidate, {"messages": []})
    assert controller.state["endpoint"]["verified"]
    assert controller.state["endpoint"]["checkpoints"] == {"2": True}
