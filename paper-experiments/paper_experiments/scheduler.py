"""Bounded spawn workers; a whole low/extended trajectory owns one worker slot."""

from __future__ import annotations

import json
import multiprocessing
import os
from collections.abc import Callable, Iterable
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from datetime import UTC, datetime
from multiprocessing.reduction import DupFd
from pathlib import Path
from typing import Any, TextIO
from uuid import uuid4

from .controller import atomic_json
from .diagnostics import error_details
from .protocol import MINI_CAMPAIGN_MODELS, MINI_CAMPAIGN_PHASES
from .resources import (
    ResourceProfile,
    docker_capacity,
    docker_execution_lock,
    execution_lock,
    preflight,
)


def _leased_call(
    function: Callable[..., Any], job: tuple[Any, ...], leases: tuple[Any, ...]
) -> Any:
    descriptors = []
    try:
        for lease in leases:
            descriptors.append(lease.detach())
        return function(*job)
    finally:
        # These descriptors share the coordinator's flock; closing must not unlock it.
        for descriptor in descriptors:
            os.close(descriptor)


def bounded_process_map(
    function: Callable[..., Any],
    jobs: Iterable[tuple[Any, ...]],
    workers: int,
    *,
    on_result: Callable[[Any], None] | None = None,
    leases: tuple[TextIO, ...] = (),
) -> list[Any]:
    """Submit at most workers jobs; no shared module patches or retry environment."""
    if workers < 1:
        raise ValueError("At least one worker is required")
    pending = iter(jobs)
    results = []
    with ProcessPoolExecutor(
        max_workers=workers, mp_context=multiprocessing.get_context("spawn")
    ) as pool:
        futures = set()
        for _ in range(workers):
            job = next(pending, None)
            if job is not None:
                futures.add(
                    pool.submit(
                        _leased_call,
                        function,
                        job,
                        tuple(DupFd(lease.fileno()) for lease in leases),
                    )
                )
        while futures:
            done, futures = wait(futures, return_when=FIRST_COMPLETED)
            for future in done:
                result = future.result()
                results.append(result)
                if on_result:
                    on_result(result)
            for _ in done:
                job = next(pending, None)
                if job is not None:
                    futures.add(
                        pool.submit(
                            _leased_call,
                            function,
                            job,
                            tuple(DupFd(lease.fileno()) for lease in leases),
                        )
                    )
    return results


def run_slot(
    run: dict[str, Any], inputs: dict[str, Any], output: Path, phase: str
) -> dict[str, Any]:
    from .cli import run_one

    started = datetime.now(UTC).isoformat()
    try:
        with execution_lock(output.parent.parent / ".run-locks" / f"{run['run_id']}.lock"):
            if registered_phase := MINI_CAMPAIGN_PHASES.get(run.get("model")):
                if phase not in {"all", registered_phase}:
                    raise ValueError(
                        f"{registered_phase.title()} runs require phase all or {registered_phase}"
                    )
                run_one(run, inputs, output, phase=registered_phase)
            elif phase == "all":
                run_one(run, inputs, output, phase="low")
                if run["extendable"]:
                    run_one(run, inputs, output, phase="extended")
            else:
                run_one(run, inputs, output, phase=phase)
    except Exception as error:
        details = error_details(error)
        return {
            "run_id": run["run_id"],
            "pid": os.getpid(),
            "started_at": started,
            "finished_at": datetime.now(UTC).isoformat(),
            "error": f"{details['type']}: {details['error']}",
            "error_details": details,
        }
    return {
        "run_id": run["run_id"],
        "pid": os.getpid(),
        "started_at": started,
        "finished_at": datetime.now(UTC).isoformat(),
        "error": None,
    }


def _state(experiment: Path, run: dict[str, Any]) -> dict[str, Any] | None:
    path = experiment / "runs" / run["run_id"] / "controller.json"
    return json.loads(path.read_text()) if path.exists() else None


def _container(state: dict[str, Any] | None) -> str | None:
    driver = state.get("driver_state") or {} if state else {}
    return driver.get("container_id") or driver.get("sandbox", {}).get("container_id")


def _needs_work(run: dict[str, Any], state: dict[str, Any] | None, phase: str) -> bool:
    if registered_phase := MINI_CAMPAIGN_PHASES.get(run.get("model")):
        if phase not in {"all", registered_phase}:
            raise ValueError(
                f"{registered_phase.title()} runs require phase all or {registered_phase}"
            )
        return state is None or not state["paused"]
    if phase in MINI_CAMPAIGN_MODELS:
        raise ValueError(f"The {phase} phase requires a matching campaign registration")
    if phase == "extended" and not run["extendable"]:
        return False
    if state is None:
        if phase == "extended":
            raise ValueError(f"Run {run['run_id']} has no frozen low-budget endpoint")
        return True
    if phase == "low":
        return not state["paused"] and state["phase"] != "extended"
    if not state["paused"]:
        if phase == "extended" and state["phase"] != "extended":
            raise ValueError(f"Run {run['run_id']} has no frozen low-budget endpoint")
        return True
    low = state.get("low_endpoint")
    return bool(
        run["extendable"]
        and state["phase"] == "low"
        and low
        and not low["verified"]
        and low.get("resumable", True)
        and low["stop_reason"] in {"cost", "call_count", "elapsed_time"}
    )


def run_queue(
    experiment: Path, runs: list[dict[str, Any]], inputs: dict[str, Any], phase: str
) -> list[dict[str, Any]]:
    if phase not in {"low", "extended", "all", *MINI_CAMPAIGN_MODELS}:
        raise ValueError("Unknown execution phase")
    experiment = experiment.resolve()
    profile = ResourceProfile.from_dict(inputs["resources"])
    with (
        execution_lock(experiment / ".execution.lock") as experiment_lease,
        docker_execution_lock() as docker_lease,
    ):
        states = {run["run_id"]: _state(experiment, run) for run in runs}
        pending = [run for run in runs if _needs_work(run, states[run["run_id"]], phase)]
        if not pending:
            return []
        capacity = docker_capacity()
        live = {container["Id"] for container in capacity["containers"]}
        # Existing in-memory states run first; previously unstarted runs remain FIFO.
        pending.sort(key=lambda run: _container(states[run["run_id"]]) not in live)
        workers = min(profile.workers, len(pending))
        reused = tuple(
            identifier
            for run in pending
            if (identifier := _container(states[run["run_id"]])) in live
        )
        future_retained = 0
        if phase == "low":
            designated = sum(run["extendable"] for run in pending)
            future_retained = min(designated, max(0, len(pending) - workers))
        admission = preflight(
            profile,
            workers=workers,
            future_retained=future_retained,
            reuse_containers=reused,
            capacity=capacity,
        )
        receipt = {
            "phase": phase,
            "admission": admission,
            "started_at": datetime.now(UTC).isoformat(),
            "dispatch_order": [run["run_id"] for run in pending],
            "results": [],
            "queue_policy": "registered FIFO; existing containers first; low-to-extended continuation keeps its process slot",
            "process_start_method": "spawn",
            "worker_leases": "shared admission flock descriptors plus per-run exclusion",
        }
        receipt_path = experiment / "scheduler" / f"{uuid4().hex}.json"
        atomic_json(receipt_path, receipt)

        def record(result: dict[str, Any]) -> None:
            receipt["results"].append(result)
            atomic_json(receipt_path, receipt)
            if result["error"]:
                raise RuntimeError(
                    f"Worker failed; no further jobs will start. Inspect {receipt_path}"
                )

        results = bounded_process_map(
            run_slot,
            ((run, inputs, experiment / "runs" / run["run_id"], phase) for run in pending),
            workers,
            on_result=record,
            leases=(experiment_lease, docker_lease),
        )
        receipt["finished_at"] = datetime.now(UTC).isoformat()
        atomic_json(receipt_path, receipt)
        if any(result["error"] for result in results):
            raise RuntimeError(f"Some worker runs failed; inspect {receipt_path}")
        return results
