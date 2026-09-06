"""Defer a frozen campaign until its predecessor finishes, with infrequent status reads."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import signal
import subprocess
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, TextIO

INTERVAL_SECONDS = 1800


def utc_now() -> datetime:
    return datetime.now(UTC)


def timestamp(value: str) -> datetime:
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("Timestamps must include a timezone")
    return result


def read_json(path: Path) -> dict[str, Any]:
    result = json.loads(path.read_text())
    if not isinstance(result, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return result


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def exclusive_lock(path: Path) -> Iterator[TextIO]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError(f"A watcher or its child already holds {path}") from error
        # Child processes inherit this lease; closing must not explicitly unlock it.
        yield stream


def lock_held(path: Path) -> bool:
    try:
        with path.open("rb") as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
    except FileNotFoundError:
        pass
    return False


def validate_config(config: dict[str, Any]) -> None:
    if config.get("schema_version") != 1:
        raise ValueError("Unsupported deferred configuration schema")
    if config.get("interval_seconds") != INTERVAL_SECONDS:
        raise ValueError("The inspection interval must be 1800 seconds")
    timestamp(config["first_check_at"])
    paths = [config["state_dir"]]
    for name in ("predecessor", "successor"):
        spec = config[name]
        ids = spec["run_ids"]
        if (
            not isinstance(ids, list)
            or len(ids) != 12
            or len(set(ids)) != 12
            or not all(isinstance(item, str) and re.fullmatch(r"[\w.-]+", item) for item in ids)
            or any(item in {".", ".."} for item in ids)
        ):
            raise ValueError("Each campaign requires twelve unique, safe run IDs")
        paths.append(spec["experiment"])
    paths.append(config["predecessor"]["background_status"])
    if (
        Path(config["predecessor"]["experiment"]).resolve()
        == Path(config["successor"]["experiment"]).resolve()
    ):
        raise ValueError("The successor must use a separate experiment directory")
    stages = config["stages"]
    names = [stage["name"] for stage in stages]
    if (
        not names
        or names[0] != "smoke"
        or names[-1] != "run"
        or "register" not in names
        or len(set(names)) != len(names)
        or not set(names) <= {"smoke", "validate", "register", "run"}
    ):
        raise ValueError("Stages must run smoke, optional validation, register, then run")
    if "validate" in names and names.index("validate") > names.index("register"):
        raise ValueError("Smoke validation must precede registration")
    if (
        not stages[0].get("success_json")
        or stages[0].get("success_fields", {}).get("ok") is not True
    ):
        raise ValueError("The smoke stage must require its JSON receipt's ok=true")
    for stage in stages:
        argv = stage["argv"]
        if (
            not isinstance(argv, list)
            or not argv
            or not all(isinstance(item, str) for item in argv)
        ):
            raise ValueError("Every stage requires an argv string array")
        paths.append(stage["cwd"])
        if stage.get("success_json"):
            paths.append(stage["success_json"])
    if not config.get("immutable_inputs"):
        raise ValueError("Immutable source and configuration hashes are required")
    for path, digest in config["immutable_inputs"].items():
        paths.append(path)
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("Immutable inputs require SHA-256 digests")
    if any(not isinstance(path, str) or not Path(path).is_absolute() for path in paths):
        raise ValueError("All configured paths must be absolute")


def verify_inputs(config: dict[str, Any]) -> None:
    for filename, expected in config["immutable_inputs"].items():
        if hashlib.sha256(Path(filename).read_bytes()).hexdigest() != expected:
            raise ValueError(f"Changed immutable input: {filename}")


def new_state(config: dict[str, Any], config_sha256: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "config_sha256": config_sha256,
        "status": "waiting",
        "next_check_at": config["first_check_at"],
        "stage_index": 0,
        "stage_receipts": [],
    }


def save_state(config: dict[str, Any], state: dict[str, Any]) -> None:
    atomic_json(Path(config["state_dir"]) / "state.json", state)


def load_state(config: dict[str, Any], digest: str) -> dict[str, Any]:
    path = Path(config["state_dir"]) / "state.json"
    state = read_json(path) if path.exists() else new_state(config, digest)
    if state.get("config_sha256") != digest:
        raise ValueError("The deferred configuration changed after initialization")
    if state["status"] == "stage_running":
        state.update(
            status="blocked",
            reason="Interrupted stage: never replay a previously started command automatically",
        )
    save_state(config, state)
    return state


def campaign_snapshot(spec: dict[str, Any]) -> dict[str, Any]:
    root = Path(spec["experiment"])
    result: dict[str, Any] = {
        "total": len(spec["run_ids"]),
        "completed": 0,
        "solved": 0,
        "active": 0,
        "queued": 0,
        "accounting_complete": True,
    }
    charge = Decimal(0)
    for run_id in spec["run_ids"]:
        path = root / "runs" / run_id / "controller.json"
        if not path.exists():
            result["queued"] += 1
            continue
        state = read_json(path)
        if state.get("run", {}).get("run_id") != run_id:
            raise ValueError(f"Controller does not match registered run {run_id}")
        endpoint = state.get("endpoint")
        completed = state.get("paused") is True and isinstance(endpoint, dict) and bool(endpoint)
        result["completed"] += int(completed)
        result["active"] += int(not completed)
        result["solved"] += int(completed and endpoint.get("verified") is True)
        ledger = state.get("ledger", {})
        amount = ledger.get("known_charge_usd")
        if amount is None:
            amount = ledger.get("charge_usd") or "0"
        value = Decimal(str(amount))
        if not value.is_finite() or value < 0:
            raise ValueError(f"Invalid recorded spending for {run_id}")
        charge += value
        result["accounting_complete"] &= ledger.get("accounting_complete") is True
    locks = [
        root / ".execution.lock",
        *(root / ".run-locks" / f"{item}.lock" for item in spec["run_ids"]),
    ]
    result["pending_claims"] = [str(path) for path in locks if lock_held(path)]
    result["known_charge_usd"] = str(charge)
    result["terminal"] = result["completed"] == result["total"] and not result["pending_claims"]
    return result


def predecessor_action(status: dict[str, Any], snapshot: dict[str, Any]) -> str:
    if status.get("status") == "running":
        return "waiting"
    if (
        status.get("status") == "finished"
        and type(status.get("returncode")) is int
        and status["returncode"] == 0
        and snapshot["terminal"]
    ):
        return "ready"
    return "blocked"


def record_progress(
    config: dict[str, Any],
    state: dict[str, Any],
    campaign: str,
    snapshot: dict[str, Any],
    now: datetime,
) -> None:
    event = {"event": "progress", "campaign": campaign, "checked_at": now.isoformat(), **snapshot}
    state["last_progress"] = event
    save_state(config, state)
    with (Path(config["state_dir"]) / "progress.jsonl").open("a") as stream:
        stream.write(json.dumps(event, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    print(json.dumps(event, sort_keys=True), flush=True)


def block(config: dict[str, Any], state: dict[str, Any], reason: str) -> int:
    state.update(status="blocked", reason=reason, stopped_at=utc_now().isoformat())
    save_state(config, state)
    print(json.dumps({"event": "blocked", "reason": reason}), flush=True)
    return 1


def tick(config: dict[str, Any], state: dict[str, Any], now: datetime) -> str:
    if state["status"] != "waiting" or now < timestamp(state["next_check_at"]):
        return state["status"]
    # Persist the next deadline before reading the predecessor, including on crashes.
    state["next_check_at"] = (now + timedelta(seconds=INTERVAL_SECONDS)).isoformat()
    save_state(config, state)
    try:
        status = read_json(Path(config["predecessor"]["background_status"]))
        snapshot = campaign_snapshot(config["predecessor"])
        record_progress(config, state, "predecessor", snapshot, now)
        state["predecessor_status"] = status
        state["status"] = predecessor_action(status, snapshot)
        if state["status"] == "blocked":
            state["reason"] = "Predecessor failed, is incomplete, or retains worker claims"
        save_state(config, state)
    except (OSError, ValueError, KeyError, TypeError) as error:
        block(config, state, f"Cannot validate predecessor completion: {type(error).__name__}")
    return state["status"]


def validate_stage_receipt(stage: dict[str, Any]) -> None:
    if not stage.get("success_json"):
        return
    receipt = read_json(Path(stage["success_json"]))
    for field, expected in stage.get("success_fields", {}).items():
        value: Any = receipt
        for part in field.split("."):
            value = value[part]
        if value != expected or type(value) is not type(expected):
            raise ValueError(f"Stage {stage['name']} receipt failed field {field}")


def terminate_child(child: subprocess.Popen[Any]) -> None:
    if child.poll() is not None:
        return
    try:
        os.killpg(child.pid, signal.SIGTERM)
        child.wait(timeout=30)
    except subprocess.TimeoutExpired:
        os.killpg(child.pid, signal.SIGKILL)
        child.wait(timeout=30)
    except ProcessLookupError:
        pass


def await_check(state: dict[str, Any]) -> datetime:
    while (delay := (timestamp(state["next_check_at"]) - utc_now()).total_seconds()) > 0:
        time.sleep(min(delay, 60))
    return utc_now()


def finalize_successor(config: dict[str, Any], state: dict[str, Any]) -> int:
    now = await_check(state)
    state["next_check_at"] = (now + timedelta(seconds=INTERVAL_SECONDS)).isoformat()
    save_state(config, state)
    try:
        snapshot = campaign_snapshot(config["successor"])
        record_progress(config, state, "successor", snapshot, now)
        if not snapshot["terminal"]:
            return block(
                config, state, "Successor exited before all twelve runs were terminal or released"
            )
    except (OSError, ValueError, KeyError, TypeError) as error:
        return block(config, state, f"Cannot validate successor completion: {type(error).__name__}")
    state.update(status="finished", finished_at=now.isoformat())
    save_state(config, state)
    return 0


def run_stages(config: dict[str, Any], state: dict[str, Any], lease_fd: int) -> int:
    if state["status"] != "ready":
        return 0 if state["status"] == "finished" else 1
    for index in range(state["stage_index"], len(config["stages"])):
        stage = config["stages"][index]
        child = None
        try:
            verify_inputs(config)
            state.update(
                status="stage_running",
                stage=stage["name"],
                stage_index=index,
                stage_started_at=utc_now().isoformat(),
            )
            save_state(config, state)
            log_path = Path(config["state_dir"]) / f"{stage['name']}.log"
            with log_path.open("ab") as log:
                child = subprocess.Popen(
                    stage["argv"],
                    cwd=stage["cwd"],
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    pass_fds=(lease_fd,),
                )
                state["child_pid"] = child.pid
                state["next_check_at"] = (
                    utc_now() + timedelta(seconds=INTERVAL_SECONDS)
                ).isoformat()
                save_state(config, state)
                if stage["name"] != "run":
                    returncode = child.wait()
                else:
                    while True:
                        timeout = max(
                            0, (timestamp(state["next_check_at"]) - utc_now()).total_seconds()
                        )
                        try:
                            returncode = child.wait(timeout=timeout)
                            break
                        except subprocess.TimeoutExpired:
                            now = utc_now()
                            if now < timestamp(state["next_check_at"]):
                                continue
                            state["next_check_at"] = (
                                now + timedelta(seconds=INTERVAL_SECONDS)
                            ).isoformat()
                            save_state(config, state)
                            snapshot = campaign_snapshot(config["successor"])
                            record_progress(config, state, "successor", snapshot, now)
            receipt = {
                "stage": stage["name"],
                "started_at": state["stage_started_at"],
                "finished_at": utc_now().isoformat(),
                "returncode": returncode,
                "pid": child.pid,
                "log": str(log_path),
            }
            state["stage_receipts"].append(receipt)
            save_state(config, state)
            if returncode:
                return block(
                    config,
                    state,
                    f"Stage {stage['name']} exited with status {returncode}; no retry",
                )
            validate_stage_receipt(stage)
            if stage["name"] == "run":
                state.update(status="awaiting_final_check", stage_index=index + 1, child_pid=None)
                save_state(config, state)
                return finalize_successor(config, state)
            state.update(status="ready", stage_index=index + 1, child_pid=None)
            save_state(config, state)
        except BaseException as error:
            if child is not None:
                terminate_child(child)
            block(config, state, f"Stage {stage['name']} stopped: {type(error).__name__}; no retry")
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            return 1
    return block(config, state, "Missing final successor validation")


def run_deferred(config_path: Path) -> int:
    raw = config_path.read_bytes()
    config = json.loads(raw)
    validate_config(config)
    digest = hashlib.sha256(raw).hexdigest()
    with exclusive_lock(Path(config["state_dir"]) / "watcher.lock") as lease:
        state = load_state(config, digest)
        while state["status"] == "waiting":
            delay = (timestamp(state["next_check_at"]) - utc_now()).total_seconds()
            if delay > 0:
                time.sleep(min(delay, 60))
                continue
            tick(config, state, utc_now())
        if state["status"] == "ready":
            return run_stages(config, state, lease.fileno())
        if state["status"] == "awaiting_final_check":
            return finalize_successor(config, state)
        return 0 if state["status"] == "finished" else 1


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args()

    def interrupted(signum: int, frame: Any) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    raise SystemExit(run_deferred(args.config))
