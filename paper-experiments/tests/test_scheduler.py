import fcntl
import json
import os
import tempfile
import time
from pathlib import Path

import pytest

from paper_experiments import cli, scheduler
from paper_experiments.protocol import register_runs
from paper_experiments.resources import GIB, ResourceProfile


@pytest.fixture(autouse=True)
def isolated_machine_lock(monkeypatch):
    """Separate test leases from live campaigns, including spawned workers."""
    with tempfile.TemporaryDirectory(prefix="ml-sch-", dir="/tmp") as temporary:
        monkeypatch.setenv("TMPDIR", temporary)
        monkeypatch.setattr(tempfile, "tempdir", temporary)
        yield


def simultaneous_job(root, number):
    root = Path(root)
    with (root / "lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = json.loads((root / "state.json").read_text())
        state["active"] += 1
        state["maximum"] = max(state["maximum"], state["active"])
        (root / "state.json").write_text(json.dumps(state))
    time.sleep(0.08)
    with (root / "lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = json.loads((root / "state.json").read_text())
        state["active"] -= 1
        (root / "state.json").write_text(json.dumps(state))
    return {"pid": os.getpid(), "number": number}


def test_real_spawn_workers_are_bounded_and_separate_from_parent(tmp_path):
    (tmp_path / "state.json").write_text(json.dumps({"active": 0, "maximum": 0}))
    results = scheduler.bounded_process_map(
        simultaneous_job, [(str(tmp_path), number) for number in range(6)], 2
    )
    assert {result["number"] for result in results} == set(range(6))
    assert len({result["pid"] for result in results}) == 2
    assert all(result["pid"] != os.getpid() for result in results)
    assert json.loads((tmp_path / "state.json").read_text()) == {"active": 0, "maximum": 2}


def test_designated_continuation_keeps_same_process_slot(tmp_path, monkeypatch):
    run = next(run for run in register_runs(0)["runs"] if run["extendable"])
    events = []

    def one(record, inputs, output, *, phase):
        events.append((record["run_id"], phase, os.getpid(), output))

    monkeypatch.setattr(cli, "run_one", one)
    result = scheduler.run_slot(run, {}, tmp_path, "all")
    assert result["error"] is None
    assert [event[1] for event in events] == ["low", "extended"]
    assert events[0][2:] == events[1][2:]


def test_precontroller_failure_preserves_erange_trace_without_secrets(tmp_path, monkeypatch):
    import errno

    run = register_runs(0)["runs"][0]
    output = tmp_path / "runs" / run["run_id"]
    secret = "fake-test-scheduler-credential"
    monkeypatch.setenv("OPENROUTER_API_KEY", secret)
    failed_path = str(tmp_path / "bundle" / "Module.hash")

    def failed_scan(*args, **kwargs):
        raise OSError(errno.ERANGE, f"request Bearer {secret}", failed_path)

    monkeypatch.setattr(cli, "run_one", failed_scan)
    result = scheduler.run_slot(run, {}, output, "all")
    diagnostics = result["error_details"]
    assert diagnostics["errno"] == errno.ERANGE
    assert diagnostics["errno_name"] == "ERANGE"
    assert diagnostics["filename"] == failed_path
    assert diagnostics["traceback"][-1]["function"] == "failed_scan"
    assert secret not in json.dumps(result)
    assert result["error"]
    assert not output.exists()


def test_low_only_memory_admission_rejects_before_starting_any_worker(tmp_path, monkeypatch):
    monkeypatch.setattr(
        scheduler,
        "docker_capacity",
        lambda: {"memory_bytes": 24 * GIB, "cpus": 14, "containers": []},
    )
    monkeypatch.setattr(
        scheduler,
        "bounded_process_map",
        lambda *args, **kwargs: pytest.fail("model worker started before admission"),
    )
    with pytest.raises(RuntimeError, match="38 GiB"):
        scheduler.run_queue(
            tmp_path,
            register_runs(0)["runs"],
            {"resources": ResourceProfile.load().to_dict()},
            "low",
        )


@pytest.mark.parametrize("worker_count,required_gib", [(1, 14), (2, 26)])
def test_all_phase_reserves_only_configured_agent_evaluator_pairs(
    tmp_path, monkeypatch, worker_count, required_gib
):
    monkeypatch.setattr(
        scheduler,
        "docker_capacity",
        lambda: {"memory_bytes": 28 * GIB, "cpus": 14, "containers": []},
    )
    seen = []

    def launch(function, jobs, workers, **kwargs):
        seen.extend(jobs)
        assert workers == worker_count
        assert function is scheduler.run_slot
        return []

    monkeypatch.setattr(scheduler, "bounded_process_map", launch)
    profile = ResourceProfile.load().to_dict() | {"workers": worker_count}
    scheduler.run_queue(tmp_path, register_runs(0)["runs"], {"resources": profile}, "all")
    assert len(seen) == 36
    receipt = json.loads(next((tmp_path / "scheduler").glob("*.json")).read_text())
    assert receipt["admission"]["required_memory_bytes"] == required_gib * GIB
    assert receipt["dispatch_order"] == [run["run_id"] for run in register_runs(0)["runs"]]
    assert all(job[-1] == "all" for job in seen)


def failing_job(root, number):
    with (Path(root) / "dispatched").open("a") as stream:
        stream.write(str(number) + "\n")
    return {"error": "failed"}


def test_failed_worker_stops_queue_before_next_dispatch(tmp_path):
    def reject(result):
        raise RuntimeError("stop after failed worker")

    with pytest.raises(RuntimeError, match="stop after failed"):
        scheduler.bounded_process_map(
            failing_job, [(str(tmp_path), number) for number in range(3)], 1, on_result=reject
        )
    assert (tmp_path / "dispatched").read_text().splitlines() == ["0"]


def orphaned_worker(run, inputs, output, phase):
    root = Path(output).parent.parent
    (root / "worker-started.json").write_text(json.dumps({"pid": os.getpid()}))
    deadline = time.monotonic() + 15
    while not (root / "release-worker").exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    return {"run_id": run["run_id"], "error": None}


def killable_coordinator(root):
    root = Path(root)
    scheduler.docker_capacity = lambda: {"memory_bytes": 24 * GIB, "cpus": 14, "containers": []}
    scheduler.run_slot = orphaned_worker
    scheduler.run_queue(
        root,
        [{"run_id": "lease-run", "extendable": False}],
        {"resources": ResourceProfile.load().to_dict()},
        "all",
    )


def test_killed_coordinator_keeps_admission_locked_while_worker_runs(tmp_path):
    import multiprocessing

    from paper_experiments.resources import docker_execution_lock, execution_lock

    coordinator = multiprocessing.get_context("spawn").Process(
        target=killable_coordinator, args=(str(tmp_path),)
    )
    coordinator.start()
    try:
        deadline = time.monotonic() + 10
        while not (tmp_path / "worker-started.json").exists() and time.monotonic() < deadline:
            if not coordinator.is_alive():
                break
            time.sleep(0.01)
        assert (tmp_path / "worker-started.json").exists()
        coordinator.kill()
        coordinator.join(5)
        assert not coordinator.is_alive()
        with (
            pytest.raises(RuntimeError, match="Another experiment"),
            execution_lock(tmp_path / ".execution.lock"),
        ):
            pytest.fail("coordinator death released the experiment lease")
        with pytest.raises(RuntimeError, match="Another experiment"), docker_execution_lock():
            pytest.fail("coordinator death released the machine lease")
    finally:
        (tmp_path / "release-worker").touch()
        if coordinator.is_alive():
            coordinator.kill()
        coordinator.join(5)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                with execution_lock(tmp_path / ".execution.lock"):
                    break
            except RuntimeError:
                time.sleep(0.01)
        else:
            pytest.fail("worker did not release its completed admission lease")


def test_per_run_lease_excludes_duplicate_worker_before_harness_work(tmp_path, monkeypatch):
    from paper_experiments.resources import execution_lock

    run = register_runs(0)["runs"][0]
    output = tmp_path / "runs" / run["run_id"]
    monkeypatch.setattr(
        cli, "run_one", lambda *args, **kwargs: pytest.fail("duplicate harness started")
    )
    with execution_lock(tmp_path / ".run-locks" / f"{run['run_id']}.lock"):
        result = scheduler.run_slot(run, {}, output, "all")
    assert "Another experiment" in result["error"]


@pytest.mark.parametrize("campaign", ["astra", "fable"])
def test_mini_campaign_all_phase_runs_once_without_low_or_extended(tmp_path, monkeypatch, campaign):
    run = register_runs(2025, campaign=campaign)["runs"][0]
    dispatched = []
    monkeypatch.setattr(
        cli, "run_one", lambda record, inputs, output, *, phase: dispatched.append(phase)
    )
    result = scheduler.run_slot(run, {}, tmp_path / "runs" / run["run_id"], "all")
    assert result["error"] is None
    assert dispatched == [campaign]
    assert scheduler._needs_work(run, None, "all")
    assert scheduler._needs_work(run, {"phase": campaign, "paused": False}, campaign)
    assert not scheduler._needs_work(run, {"phase": campaign, "paused": True}, "all")
    with pytest.raises(ValueError, match=f"{campaign.title()} runs require"):
        scheduler._needs_work(run, None, "low")
    other_campaign = "fable" if campaign == "astra" else "astra"
    with pytest.raises(ValueError, match=f"{campaign.title()} runs require"):
        scheduler._needs_work(run, None, other_campaign)
    rejected = scheduler.run_slot(run, {}, tmp_path / "runs" / run["run_id"], other_campaign)
    assert "require phase" in rejected["error"]
    assert dispatched == [campaign]


@pytest.mark.parametrize("campaign", ["astra", "fable"])
def test_mini_campaign_queue_dispatches_twelve_runs_to_two_workers(tmp_path, monkeypatch, campaign):
    monkeypatch.setattr(
        scheduler,
        "docker_capacity",
        lambda: {"memory_bytes": 28 * GIB, "cpus": 14, "containers": []},
    )
    jobs = []

    def launch(function, pending, workers, **kwargs):
        assert workers == 2
        jobs.extend(pending)
        return []

    monkeypatch.setattr(scheduler, "bounded_process_map", launch)
    profile = ResourceProfile.load().to_dict() | {"workers": 2}
    runs = register_runs(2025, campaign=campaign)["runs"]
    scheduler.run_queue(tmp_path, runs, {"resources": profile}, "all")
    assert [job[0] for job in jobs] == runs
    assert len(jobs) == 12
    assert all(job[-1] == "all" for job in jobs)
