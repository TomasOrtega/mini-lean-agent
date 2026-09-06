import hashlib
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from paper_experiments import deferred

NOW = datetime(2026, 9, 5, 20, 45, 6, tzinfo=UTC)


@pytest.fixture(autouse=True)
def scheduled_wait_without_wall_time(monkeypatch):
    monkeypatch.setattr(
        deferred, "await_check", lambda state: deferred.timestamp(state["next_check_at"])
    )


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def campaign(tmp_path, name="predecessor", complete=True):
    root = tmp_path / name
    ids = [f"run-{number}" for number in range(12)]
    for index, run_id in enumerate(ids):
        write_json(
            root / "runs" / run_id / "controller.json",
            {
                "run": {"run_id": run_id},
                "paused": complete,
                "endpoint": {"verified": index < 3} if complete else None,
                "ledger": {"charge_usd": "1.25", "accounting_complete": True},
            },
        )
    status = tmp_path / f"{name}-background.json"
    write_json(status, {"status": "finished", "returncode": 0})
    return {"experiment": str(root), "run_ids": ids, "background_status": str(status)}


def settings(tmp_path):
    frozen = tmp_path / "frozen.txt"
    frozen.write_text("registered settings")
    return {
        "schema_version": 1,
        "interval_seconds": 1800,
        "first_check_at": NOW.isoformat(),
        "state_dir": str(tmp_path / "handoff"),
        "predecessor": campaign(tmp_path),
        "successor": campaign(tmp_path, "successor"),
        "immutable_inputs": {str(frozen): hashlib.sha256(frozen.read_bytes()).hexdigest()},
        "stages": [
            {
                "name": "smoke",
                "argv": [sys.executable, "-c", "pass"],
                "cwd": str(tmp_path),
                "success_json": str(tmp_path / "smoke.json"),
                "success_fields": {"ok": True},
            },
            {"name": "register", "argv": [sys.executable, "-c", "pass"], "cwd": str(tmp_path)},
            {"name": "run", "argv": [sys.executable, "-c", "pass"], "cwd": str(tmp_path)},
        ],
    }


def initial_state(config):
    return deferred.new_state(config, "config-sha")


def test_tick_never_inspects_before_due_time(tmp_path, monkeypatch):
    config = settings(tmp_path)
    state = initial_state(config)
    monkeypatch.setattr(
        deferred, "campaign_snapshot", lambda *_: pytest.fail("premature inspection")
    )
    assert deferred.tick(config, state, NOW - timedelta(microseconds=1)) == "waiting"
    assert state["next_check_at"] == NOW.isoformat()


def test_running_predecessor_waits_exactly_thirty_minutes_and_survives_restart(tmp_path):
    config = settings(tmp_path)
    write_json(Path(config["predecessor"]["background_status"]), {"status": "running"})
    state = initial_state(config)
    assert deferred.tick(config, state, NOW) == "waiting"
    assert state["next_check_at"] == (NOW + timedelta(seconds=1800)).isoformat()
    persisted = json.loads((Path(config["state_dir"]) / "state.json").read_text())
    assert deferred.tick(config, persisted, NOW + timedelta(seconds=1799)) == "waiting"


def test_late_tick_schedules_from_now_without_catchup_polls(tmp_path):
    config = settings(tmp_path)
    write_json(Path(config["predecessor"]["background_status"]), {"status": "running"})
    state = initial_state(config)
    later = NOW + timedelta(hours=4)
    deferred.tick(config, state, later)
    assert state["next_check_at"] == (later + timedelta(seconds=1800)).isoformat()


def test_zero_exit_and_all_twelve_terminal_endpoints_allow_handoff(tmp_path):
    config = settings(tmp_path)
    assert deferred.tick(config, initial_state(config), NOW) == "ready"


@pytest.mark.parametrize(
    "status",
    [
        {"status": "failed", "returncode": 1},
        {"status": "launcher_failed", "error_type": "OSError"},
        {"status": "finished", "returncode": 2},
        {"status": "finished"},
    ],
)
def test_failed_or_ambiguous_predecessor_blocks(tmp_path, status):
    config = settings(tmp_path)
    write_json(Path(config["predecessor"]["background_status"]), status)
    assert deferred.tick(config, initial_state(config), NOW) == "blocked"


@pytest.mark.parametrize("damage", ["missing", "unpaused", "endpoint"])
def test_finished_background_cannot_hide_incomplete_problem(tmp_path, damage):
    config = settings(tmp_path)
    path = Path(config["predecessor"]["experiment"]) / "runs/run-0/controller.json"
    if damage == "missing":
        path.unlink()
    else:
        value = json.loads(path.read_text())
        value["paused" if damage == "unpaused" else "endpoint"] = (
            False if damage == "unpaused" else None
        )
        write_json(path, value)
    assert deferred.tick(config, initial_state(config), NOW) == "blocked"


def test_held_worker_claim_blocks_even_when_controller_is_paused(tmp_path):
    config = settings(tmp_path)
    lock_path = Path(config["predecessor"]["experiment"]) / ".run-locks/run-0.lock"
    with deferred.exclusive_lock(lock_path):
        assert deferred.tick(config, initial_state(config), NOW) == "blocked"


def test_progress_records_known_spending_with_unavailable_charge(tmp_path):
    spec = campaign(tmp_path)
    path = Path(spec["experiment"]) / "runs/run-0/controller.json"
    state = json.loads(path.read_text())
    state["ledger"] = {"charge_usd": None, "known_charge_usd": "0.10", "accounting_complete": False}
    write_json(path, state)
    snapshot = deferred.campaign_snapshot(spec)
    assert snapshot["solved"] == 3
    assert snapshot["completed"] == 12
    assert snapshot["known_charge_usd"] == "13.85"
    assert snapshot["accounting_complete"] is False


def test_duplicate_watchers_are_rejected(tmp_path):
    with (
        deferred.exclusive_lock(tmp_path / "watcher.lock"),
        pytest.raises(RuntimeError, match="already"),
        deferred.exclusive_lock(tmp_path / "watcher.lock"),
    ):
        pytest.fail("duplicate watcher acquired lock")


def test_changed_immutable_input_is_rejected_before_any_command(tmp_path):
    config = settings(tmp_path)
    Path(next(iter(config["immutable_inputs"]))).write_text("changed")
    with pytest.raises(ValueError, match="Changed immutable"):
        deferred.verify_inputs(config)


def test_state_refuses_changed_config_or_prior_paid_stage(tmp_path):
    config = settings(tmp_path)
    state_dir = Path(config["state_dir"])
    state = initial_state(config)
    write_json(state_dir / "state.json", state)
    with pytest.raises(ValueError, match="configuration"):
        deferred.load_state(config, "other-sha")
    state["status"] = "stage_running"
    state["stage"] = "smoke"
    write_json(state_dir / "state.json", state)
    recovered = deferred.load_state(config, "config-sha")
    assert recovered["status"] == "blocked"
    assert "never replay" in recovered["reason"]


def test_smoke_validation_failure_never_starts_registration(tmp_path, monkeypatch):
    config = settings(tmp_path)
    write_json(tmp_path / "smoke.json", {"ok": False})
    state = initial_state(config)
    state["status"] = "ready"
    called = []

    original = subprocess.Popen

    def spawn(*args, **kwargs):
        called.append(args[0])
        return original([sys.executable, "-c", "pass"], **kwargs)

    monkeypatch.setattr(deferred.subprocess, "Popen", spawn)
    with deferred.exclusive_lock(Path(config["state_dir"]) / "watcher.lock") as lease:
        assert deferred.run_stages(config, state, lease.fileno()) == 1
    assert len(called) == 1
    assert state["status"] == "blocked"


def test_successful_stages_run_sequentially_without_polling_delay(tmp_path, monkeypatch):
    config = settings(tmp_path)
    write_json(tmp_path / "smoke.json", {"ok": True})
    state = initial_state(config)
    state["status"] = "ready"
    monkeypatch.setattr(
        deferred, "time", SimpleNamespace(sleep=lambda *_: pytest.fail("setup-stage sleep"))
    )
    with deferred.exclusive_lock(Path(config["state_dir"]) / "watcher.lock") as lease:
        assert deferred.run_stages(config, state, lease.fileno()) == 0
    assert state["status"] == "finished"
    assert [receipt["stage"] for receipt in state["stage_receipts"]] == ["smoke", "register", "run"]
    assert all(receipt["returncode"] == 0 for receipt in state["stage_receipts"])


def test_failed_paid_command_is_not_replayed(tmp_path):
    config = settings(tmp_path)
    config["stages"][0]["argv"] = [sys.executable, "-c", "raise SystemExit(7)"]
    state = initial_state(config)
    state["status"] = "ready"
    with deferred.exclusive_lock(Path(config["state_dir"]) / "watcher.lock") as lease:
        assert deferred.run_stages(config, state, lease.fileno()) == 1
        assert deferred.run_stages(config, state, lease.fileno()) == 1
    assert len(state["stage_receipts"]) == 1
    assert state["stage_receipts"][0]["returncode"] == 7


def test_successor_zero_exit_still_requires_complete_controllers(tmp_path):
    config = settings(tmp_path)
    write_json(tmp_path / "smoke.json", {"ok": True})
    (Path(config["successor"]["experiment"]) / "runs/run-0/controller.json").unlink()
    state = initial_state(config)
    state["status"] = "ready"
    with deferred.exclusive_lock(Path(config["state_dir"]) / "watcher.lock") as lease:
        assert deferred.run_stages(config, state, lease.fileno()) == 1
    assert state["status"] == "blocked"


def test_configuration_enforces_twelve_unique_runs_and_thirty_minute_interval(tmp_path):
    config = settings(tmp_path)
    deferred.validate_config(config)
    config["interval_seconds"] = 1
    with pytest.raises(ValueError, match="1800"):
        deferred.validate_config(config)
    config["interval_seconds"] = 1800
    config["predecessor"]["run_ids"][-1] = "run-0"
    with pytest.raises(ValueError, match="twelve"):
        deferred.validate_config(config)


def test_stage_is_persisted_before_child_launch_and_child_inherits_lease(tmp_path, monkeypatch):
    config = settings(tmp_path)
    write_json(tmp_path / "smoke.json", {"ok": True})
    state = initial_state(config)
    state["status"] = "ready"
    original = subprocess.Popen
    observations = []

    def spawn(argv, **kwargs):
        saved = json.loads((Path(config["state_dir"]) / "state.json").read_text())
        assert saved["status"] == "stage_running"
        assert len(kwargs["pass_fds"]) == 1
        assert kwargs["start_new_session"] is True
        observations.append(saved["stage"])
        return original(argv, **kwargs)

    monkeypatch.setattr(deferred.subprocess, "Popen", spawn)
    with deferred.exclusive_lock(Path(config["state_dir"]) / "watcher.lock") as lease:
        assert deferred.run_stages(config, state, lease.fileno()) == 0
    assert observations == ["smoke", "register", "run"]


def test_child_holds_watcher_lock_after_parent_closes_its_copy(tmp_path):
    lock_path = tmp_path / "watcher.lock"
    child = None
    try:
        with deferred.exclusive_lock(lock_path) as lease:
            child = subprocess.Popen(
                [sys.executable, "-c", "import sys; sys.stdin.buffer.read(1)"],
                stdin=subprocess.PIPE,
                pass_fds=(lease.fileno(),),
            )
        with pytest.raises(RuntimeError, match="already"), deferred.exclusive_lock(lock_path):
            pytest.fail("orphaned child lost its lease")
    finally:
        if child is not None:
            child.communicate(b"x", timeout=5)
    with deferred.exclusive_lock(lock_path):
        pass


def test_successor_exit_waits_for_next_thirty_minute_inspection(tmp_path, monkeypatch):
    config = settings(tmp_path)
    write_json(tmp_path / "smoke.json", {"ok": True})
    state = initial_state(config)
    state["status"] = "ready"
    clock = [NOW]
    checks = []
    snapshot = deferred.campaign_snapshot
    monkeypatch.setattr(deferred, "utc_now", lambda: clock[0])

    def wait_for_deadline(state):
        assert state["status"] == "awaiting_final_check"
        assert state["stage_receipts"][-1]["returncode"] == 0
        clock[0] = deferred.timestamp(state["next_check_at"])
        return clock[0]

    monkeypatch.setattr(deferred, "await_check", wait_for_deadline)

    def inspect(spec):
        checks.append(clock[0])
        return snapshot(spec)

    class Child:
        pid = 99999

        def __init__(self, *args, **kwargs):
            self.calls = 0

        def wait(self, timeout=None):
            if timeout is None:
                return 0
            self.calls += 1
            if self.calls == 1:
                assert timeout == 1800
                clock[0] += timedelta(seconds=1800)
                raise subprocess.TimeoutExpired("fixture", timeout)
            clock[0] += timedelta(seconds=2)
            return 0

    monkeypatch.setattr(deferred, "campaign_snapshot", inspect)
    monkeypatch.setattr(deferred.subprocess, "Popen", Child)
    with deferred.exclusive_lock(Path(config["state_dir"]) / "watcher.lock") as lease:
        assert deferred.run_stages(config, state, lease.fileno()) == 0
    assert checks == [NOW + timedelta(seconds=1800), NOW + timedelta(seconds=3600)]


def test_restart_during_final_wait_never_replays_any_paid_stage(tmp_path, monkeypatch):
    config = settings(tmp_path)
    path = tmp_path / "config.json"
    write_json(path, config)
    state = deferred.new_state(config, hashlib.sha256(path.read_bytes()).hexdigest())
    state.update(status="awaiting_final_check", stage_index=3)
    write_json(Path(config["state_dir"]) / "state.json", state)
    monkeypatch.setattr(
        deferred.subprocess, "Popen", lambda *_args, **_kwargs: pytest.fail("paid replay")
    )
    assert deferred.run_deferred(path) == 0
