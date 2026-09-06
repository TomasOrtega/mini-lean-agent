"""Supervise harness boundaries without implementing either harness's agent loop."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from .ledger import AccountingIncomplete, Ledger
from .protocol import (
    ANCHOR_CHECKPOINTS,
    EXTENDED_CHECKPOINTS,
    LOW_CHECKPOINTS,
    MINI_CAMPAIGN_CHECKPOINTS,
    MINI_CAMPAIGN_PHASES,
    OPUS,
    Limits,
    canonical_json,
    content_hash,
)
from .resources import ContainerCleanupError


class RunStopped(RuntimeError):
    pass


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(handle, "w") as stream:
            stream.write(canonical_json(value) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class RunController:
    """Persist immutable snapshots, low endpoints, and cumulative continuation limits.

    Drivers call before_request before *each* provider attempt and after_boundary only
    after tool effects and message updates have settled. Driver state must include all
    provider reasoning/tool state, configuration, and a durable workspace reference.
    """

    def __init__(
        self,
        run: dict[str, Any],
        output_dir: Path,
        ledger: Ledger,
        evaluator: Callable[[Path], dict[str, Any]],
        *,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.output_dir / "controller.json"
        self.ledger, self.evaluator, self.clock = ledger, evaluator, clock
        self._session_started = clock()
        now = datetime.now(UTC)
        if self.path.exists():
            self.state = json.loads(self.path.read_text())
            if self.state["run"] != run or self.state["price_table_sha256"] != ledger.prices.sha256:
                raise ValueError("Cannot resume a changed registration or price table")
            self._validate_ledger_prefix(self.state["ledger"], ledger.snapshot())
            self._active_base = float(self.state["active_seconds"])
            if not self.state["paused"]:
                self._active_base += max(
                    0, (now - datetime.fromisoformat(self.state["saved_at"])).total_seconds()
                )
                self.state["recovery_note"] = (
                    "Unclean interruption time conservatively included as active time"
                )
        else:
            self._active_base = 0.0
            self.state = {
                "schema_version": 1,
                "run": run,
                "price_table_sha256": ledger.prices.sha256,
                "phase": MINI_CAMPAIGN_PHASES.get(
                    run["model"], "anchor" if run["model"] == OPUS else "low"
                ),
                "started_at": now.isoformat(),
                "paused": False,
                "snapshots": [],
                "low_endpoint": None,
                "endpoint": None,
                "driver_state": None,
                "administrative_pauses": [],
            }
            self._persist()
        self._recover_boundaries()

    @staticmethod
    def _validate_ledger_prefix(saved: dict[str, Any], current: dict[str, Any]) -> None:
        if (
            current["calls"] < saved["calls"]
            or Decimal(current["known_charge_usd"]) < Decimal(saved["known_charge_usd"])
            or current["price_table_sha256"] != saved["price_table_sha256"]
        ):
            raise ValueError("Current ledger lost part of the saved accounting prefix")
        attempts = {attempt["attempt_id"]: attempt for attempt in current["attempts"]}
        for previous in saved["attempts"]:
            present = attempts.get(previous["attempt_id"])
            if present is None:
                raise ValueError("Current ledger lost a saved provider attempt")
            settled = previous["completed"] and previous.get("charge_usd") is not None
            if settled:
                if present != previous:
                    raise ValueError("Current ledger changed a settled provider attempt")
            elif any(
                present.get(key) != previous.get(key)
                for key in ("attempt_id", "role", "model", "metadata")
            ):
                raise ValueError("Current ledger changed a pending provider attempt's identity")

    @property
    def phase(self) -> str:
        return str(self.state["phase"])

    @property
    def limits(self) -> Limits:
        return Limits.for_run(self.state["run"], self.phase)

    @property
    def active_seconds(self) -> float:
        if self.state["paused"]:
            return self._active_base
        return self._active_base + max(0, self.clock() - self._session_started)

    def _persist(self) -> None:
        now = datetime.now(UTC)
        self.state["active_seconds"] = self.active_seconds
        self.state["saved_at"] = now.isoformat()
        self.state["calendar_seconds"] = (
            now - datetime.fromisoformat(self.state["started_at"])
        ).total_seconds()
        self.state["ledger"] = self.ledger.snapshot()
        atomic_json(self.path, self.state)

    def _limit_reason(self) -> str | None:
        try:
            charge = self.ledger.charge
        except AccountingIncomplete:
            return "infrastructure_failure"
        if charge >= self.limits.dollars:
            return "cost"
        if self.ledger.calls >= self.limits.calls:
            return "call_count"
        if self.active_seconds >= self.limits.active_seconds:
            return "elapsed_time"
        return None

    def before_request(
        self, candidate: Path | None = None, driver_state: dict[str, Any] | None = None
    ) -> None:
        """Check limits immediately before the ledger reserves a provider request."""
        if self.state["paused"]:
            raise RunStopped("This phase has stopped; an authorized continuation is required")
        reason = self._limit_reason()
        if reason:
            if candidate is not None and driver_state is not None:
                self.after_boundary(candidate, driver_state, boundary="request_limit")
            else:
                self._freeze(reason)
            raise RunStopped(reason)
        self._persist()

    @staticmethod
    def _eligible(snapshot: dict[str, Any], threshold: Decimal, limits: Limits) -> bool:
        charge = snapshot["charge_usd"]
        return bool(
            charge is not None
            and Decimal(charge) <= threshold
            and snapshot["calls"] <= limits.calls
            and snapshot["active_seconds"] <= limits.active_seconds
            and snapshot["verification_completed_seconds"] <= limits.active_seconds
            and snapshot["verification"].get("verified") is True
        )

    def checkpoint(self, threshold: Decimal | str, *, panel: str | None = None) -> bool:
        panel = panel or self.phase
        limits = Limits.for_run(self.state["run"], panel)
        threshold = Decimal(threshold)
        if threshold > limits.dollars or threshold < 0:
            raise ValueError("Checkpoint outside the panel's registered cost limit")
        snapshots = self.state["snapshots"]
        if panel == "low" and self.state["low_endpoint"] is not None:
            snapshots = snapshots[: self.state["low_endpoint"]["snapshot_count"]]
        return any(self._eligible(snapshot, threshold, limits) for snapshot in snapshots)

    def checkpoints(self, *, panel: str | None = None) -> dict[str, bool]:
        panel = panel or self.phase
        thresholds = {
            "low": LOW_CHECKPOINTS,
            "extended": EXTENDED_CHECKPOINTS,
            "anchor": ANCHOR_CHECKPOINTS,
            "astra": MINI_CAMPAIGN_CHECKPOINTS,
            "fable": MINI_CAMPAIGN_CHECKPOINTS,
        }[panel]
        return {
            str(threshold): self.checkpoint(threshold, panel=panel)
            for threshold in thresholds
            if threshold <= Limits.for_run(self.state["run"], panel).dollars
        }

    def after_boundary(
        self,
        candidate: Path,
        driver_state: dict[str, Any],
        *,
        stop_reason: str | None = None,
        boundary: str = "tool",
    ) -> dict[str, Any]:
        """Capture first, then independently verify the immutable bytes; never repair."""
        if self.state["paused"]:
            raise RunStopped("Cannot add artifacts to a frozen phase")
        if stop_reason not in {None, "context_capacity", "agent_exit", "infrastructure_failure"}:
            raise ValueError("Invalid driver stop reason")
        available_seconds = self.active_seconds
        ledger = self.ledger.snapshot()
        index = len(self.state["snapshots"])
        snapshot_root = self.output_dir / "snapshots"
        snapshot_root.mkdir(exist_ok=True)
        directory = snapshot_root / f"{index:06d}"
        if directory.exists():
            raise RuntimeError("Unrecovered snapshot already occupies this boundary")
        temporary = Path(tempfile.mkdtemp(prefix=".pending-", dir=snapshot_root))
        missing = not Path(candidate).is_file()
        data = b"" if missing else Path(candidate).read_bytes()
        snapshot_path = directory / "candidate.lean"
        (temporary / "candidate.lean").write_bytes(data)
        (temporary / "candidate.lean").chmod(0o444)
        atomic_json(temporary / "driver-state.json", driver_state)
        atomic_json(temporary / "ledger.json", ledger)
        snapshot = {
            "index": index,
            "phase": self.phase,
            "boundary": boundary,
            "path": str(snapshot_path),
            "sha256": hashlib.sha256(data).hexdigest(),
            "candidate_missing": missing,
            "driver_state_sha256": content_hash(driver_state),
            "charge_usd": ledger["charge_usd"],
            "calls": ledger["calls"],
            "active_seconds": available_seconds,
            "verification_completed_seconds": available_seconds,
            "verification": {"verified": False, "pending": True},
            "driver_stop_reason": stop_reason,
        }
        atomic_json(temporary / "snapshot.json", snapshot)
        os.rename(temporary, directory)
        self.state["snapshots"].append(snapshot)
        self.state["driver_state"] = json.loads(canonical_json(driver_state))
        self._persist()
        self._verify_snapshot(snapshot)
        self._finish_boundary(snapshot)
        return snapshot

    def _verify_snapshot(self, snapshot: dict[str, Any]) -> None:
        path = Path(snapshot["path"])
        if hashlib.sha256(path.read_bytes()).hexdigest() != snapshot["sha256"]:
            raise RuntimeError("Candidate snapshot failed its integrity check")
        if snapshot.get("candidate_missing"):
            verification = {"verified": False, "error": "Target was missing at this boundary"}
        else:
            try:
                verification = self.evaluator(path)
            except ContainerCleanupError:
                raise
            except Exception as error:
                verification = {
                    "verified": False,
                    "infrastructure_failure": True,
                    "error": str(error),
                }
        if hashlib.sha256(path.read_bytes()).hexdigest() != snapshot["sha256"]:
            raise RuntimeError("Evaluator mutated an immutable candidate snapshot")
        snapshot["verification"] = verification
        snapshot["verification_completed_seconds"] = self.active_seconds
        atomic_json(path.parent / "snapshot.json", snapshot)

    def _finish_boundary(self, snapshot: dict[str, Any]) -> None:
        reason = "verified" if self.checkpoint(self.limits.dollars) else self._limit_reason()
        if snapshot["verification"].get("infrastructure_failure"):
            reason = "infrastructure_failure"
        reason = reason or snapshot.get("driver_stop_reason")
        if reason:
            self._freeze(reason)
        else:
            self._persist()

    def _recover_boundaries(self) -> None:
        recovered = False
        root = self.output_dir / "snapshots"
        directories = sorted(p for p in root.iterdir() if p.name.isdigit()) if root.exists() else []
        for index, directory in enumerate(directories):
            snapshot = json.loads((directory / "snapshot.json").read_text())
            if snapshot["index"] != index or directory.name != f"{index:06d}":
                raise RuntimeError("Snapshot sequence is not contiguous")
            state = json.loads((directory / "driver-state.json").read_text())
            if content_hash(state) != snapshot["driver_state_sha256"]:
                raise RuntimeError("Frozen driver state failed its integrity check")
            if (
                hashlib.sha256((directory / "candidate.lean").read_bytes()).hexdigest()
                != snapshot["sha256"]
            ):
                raise RuntimeError("Candidate snapshot failed its integrity check")
            if index >= len(self.state["snapshots"]):
                if self.state["paused"] or snapshot["phase"] != self.phase:
                    raise RuntimeError("Unexpected snapshot after a frozen endpoint")
                self.state["snapshots"].append(snapshot)
                self.state["driver_state"] = state
                recovered = True
            elif self.state["snapshots"][index] != snapshot:
                if self.state["paused"] or not self.state["snapshots"][index]["verification"].get(
                    "pending"
                ):
                    raise RuntimeError("A settled snapshot's metadata changed")
                self.state["snapshots"][index] = snapshot
                recovered = True
            if snapshot["verification"].get("pending"):
                if self.state["paused"]:
                    raise RuntimeError("Frozen endpoint contains unverified pending work")
                self._verify_snapshot(snapshot)
                self.state["snapshots"][index] = snapshot
                recovered = True
        endpoint_path = self.output_dir / f"{self.phase}-endpoint.json"
        if not self.state["paused"] and endpoint_path.exists():
            endpoint = json.loads(endpoint_path.read_text())
            if endpoint["snapshot_count"] != len(self.state["snapshots"]) or endpoint[
                "driver_state_sha256"
            ] != content_hash(self.state["driver_state"]):
                raise RuntimeError("Interrupted endpoint freeze does not match its saved state")
            self.state["endpoint"] = endpoint
            if self.phase == "low":
                self.state["low_endpoint"] = endpoint
            self._active_base = endpoint["active_seconds"]
            self.state["paused"] = True
            self._persist()
        elif recovered:
            self._finish_boundary(self.state["snapshots"][-1])

    def _freeze(self, reason: str) -> None:
        if self.state["paused"]:
            return
        elapsed = self.active_seconds
        endpoint = {
            "phase": self.phase,
            "stop_reason": reason,
            "verified": self.checkpoint(self.limits.dollars),
            "resumable": self.state["driver_state"] is not None
            and self.state["driver_state"].get("resumable", True),
            "snapshot_count": len(self.state["snapshots"]),
            "ledger": self.ledger.snapshot(),
            "active_seconds": elapsed,
            "limits": self.limits.to_dict(),
            "checkpoints": self.checkpoints(),
            "driver_state_sha256": content_hash(self.state["driver_state"]),
            "frozen_at": datetime.now(UTC).isoformat(),
        }
        if self.phase == "low":
            self.state["low_endpoint"] = endpoint
        self.state["endpoint"] = endpoint
        self._active_base = elapsed
        self.state["paused"] = True
        atomic_json(self.output_dir / f"{self.phase}-endpoint.json", endpoint)
        self._persist()

    def resume_extended(self) -> dict[str, Any]:
        """Return the exact frozen driver state; this operation introduces no prompt."""
        endpoint = self.state["low_endpoint"]
        if self.phase != "low" or not self.state["paused"] or endpoint is None:
            raise ValueError("Continuation requires a frozen low-budget endpoint")
        if not self.state["run"]["extendable"] or endpoint["verified"]:
            raise ValueError("Only an unsolved predeclared run can continue")
        if endpoint["stop_reason"] not in {"cost", "call_count", "elapsed_time"}:
            raise ValueError("This stop is not a technically resumable budget boundary")
        if self.state["driver_state"] is None or not endpoint.get("resumable", True):
            raise ValueError("No technically resumable authentic driver state is available")
        if endpoint["ledger"] != self.ledger.snapshot():
            raise ValueError("The ledger changed during the administrative pause")
        self.state["administrative_pauses"].append(
            {
                "from": endpoint["frozen_at"],
                "to": datetime.now(UTC).isoformat(),
                "reason": "predeclared low-to-extended continuation",
            }
        )
        self.state["phase"] = "extended"
        self.state["endpoint"] = None
        self.state["paused"] = False
        self._session_started = self.clock()
        if self.checkpoint(self.limits.dollars):
            self._freeze("verified")
        else:
            self._persist()
        return json.loads(canonical_json(self.state["driver_state"]))
