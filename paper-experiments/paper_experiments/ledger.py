"""Durable, serialized accounting of every provider dispatch attempt."""

from __future__ import annotations

import fcntl
import json
import os
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from .protocol import canonical_json, content_hash

CATEGORIES = ("input", "output", "reasoning", "cache_read", "cache_write")


def dollars(value: str | int | Decimal) -> Decimal:
    result = Decimal(str(value))
    if not result.is_finite() or result < 0:
        raise ValueError("Charges and prices must be finite and non-negative")
    return result


@dataclass(frozen=True)
class Usage:
    """Provider token counters, with the inclusion conventions recorded explicitly."""

    input_tokens: int
    output_tokens: int
    reasoning_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    reasoning_in_output: bool
    cache_in_input: bool
    reasoning_reported: bool = True

    def billable(self) -> dict[str, int]:
        counters = asdict(self)
        for name in CATEGORIES:
            value = counters[f"{name}_tokens"]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError("Token counters must be non-negative integers")
        counts = {name: counters[f"{name}_tokens"] for name in CATEGORIES}
        if self.reasoning_in_output:
            counts["output"] -= counts["reasoning"]
        if self.cache_in_input:
            counts["input"] -= counts["cache_read"] + counts["cache_write"]
        if min(counts.values()) < 0:
            raise ValueError("Provider token counters contradict their inclusion convention")
        return counts


@dataclass(frozen=True)
class PriceTable:
    """Rates are USD per million disjoint tokens, including an explicit reasoning rate."""

    as_of: str
    source: str
    models: dict[str, dict[str, str]]

    def __post_init__(self) -> None:
        date.fromisoformat(self.as_of)
        if not self.source.strip() or not self.models:
            raise ValueError("A dated price table must include its source and registered models")
        for model, rates in self.models.items():
            if not model or set(rates) != set(CATEGORIES):
                raise ValueError(f"Explicit rates required for all token categories: {CATEGORIES}")
            for rate in rates.values():
                dollars(rate)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> PriceTable:
        return cls(value["as_of"], value["source"], value["models"])

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def sha256(self) -> str:
        return content_hash(self.to_dict())

    def charge(self, model: str, usage: Usage) -> Decimal:
        rates = self.models[model]
        return sum(
            (Decimal(count) * dollars(rates[name]) for name, count in usage.billable().items()),
            Decimal(0),
        ) / Decimal(1_000_000)


class AccountingIncomplete(RuntimeError):
    """An unfinished request or unavailable usage prevents safe further dispatch."""


class Ledger:
    """Append-only journal; an OS lock and one pending request prohibit overlapping spend."""

    def __init__(self, path: Path, prices: PriceTable):
        self.path = Path(path)
        self.prices = PriceTable.from_dict(json.loads(canonical_json(prices.to_dict())))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a+") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            events = self._read(stream)
            if not events:
                self._append(
                    stream, {"event": "prices", "table": prices.to_dict(), "sha256": prices.sha256}
                )
            elif events[0].get("sha256") != prices.sha256:
                raise ValueError("Cannot change the frozen price table of an existing ledger")

    @staticmethod
    def _read(stream: Any) -> list[dict[str, Any]]:
        stream.seek(0)
        return [json.loads(line) for line in stream if line.strip()]

    @staticmethod
    def _append(stream: Any, event: dict[str, Any]) -> None:
        event = {**event, "recorded_at": datetime.now(UTC).isoformat()}
        stream.seek(0, os.SEEK_END)
        stream.write(canonical_json(event) + "\n")
        stream.flush()
        os.fsync(stream.fileno())

    @staticmethod
    def _attempts(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        attempts: dict[int, dict[str, Any]] = {}
        for event in events[1:]:
            attempt_id = event["attempt_id"]
            if event["event"] == "attempt":
                attempts[attempt_id] = {**event, "completed": False}
            elif event["event"] == "result":
                attempts[attempt_id].update(event)
                attempts[attempt_id]["completed"] = True
        return list(attempts.values())

    def snapshot(self) -> dict[str, Any]:
        with self.path.open() as stream:
            fcntl.flock(stream, fcntl.LOCK_SH)
            events = self._read(stream)
        attempts = self._attempts(events)
        complete = all(
            attempt["completed"] and attempt["charge_usd"] is not None for attempt in attempts
        )
        charge = sum(
            (Decimal(a["charge_usd"]) for a in attempts if a.get("charge_usd") is not None),
            Decimal(0),
        )
        totals = dict.fromkeys(CATEGORIES, 0)
        usage_complete = True
        for attempt in attempts:
            if attempt.get("billable_tokens") is None:
                usage_complete = False
            else:
                for category, count in attempt["billable_tokens"].items():
                    totals[category] += count
        return {
            "price_table_sha256": self.prices.sha256,
            "calls": len(attempts),
            "charge_usd": str(charge) if complete else None,
            "known_charge_usd": str(charge),
            "accounting_complete": complete,
            "usage_complete": usage_complete,
            "reasoning_breakdown_complete": usage_complete
            and all(a.get("usage", {}).get("reasoning_reported", False) for a in attempts),
            "tokens": totals,
            "attempts": attempts,
            "journal_sha256": content_hash(events),
        }

    @property
    def calls(self) -> int:
        return int(self.snapshot()["calls"])

    @property
    def charge(self) -> Decimal:
        state = self.snapshot()
        if not state["accounting_complete"]:
            raise AccountingIncomplete("A pending request or unknown charge needs reconciliation")
        return Decimal(state["charge_usd"])

    def begin_attempt(self, role: str, model: str, metadata: dict[str, Any] | None = None) -> int:
        if model not in self.prices.models:
            raise ValueError(f"Unregistered model: {model}")
        with self.path.open("a+") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            attempts = self._attempts(self._read(stream))
            if any(not a["completed"] or a["charge_usd"] is None for a in attempts):
                raise AccountingIncomplete("Reconcile the previous attempt before another dispatch")
            attempt_id = len(attempts) + 1
            self._append(
                stream,
                {
                    "event": "attempt",
                    "attempt_id": attempt_id,
                    "role": role,
                    "model": model,
                    "metadata": metadata or {},
                },
            )
        return attempt_id

    def finish_attempt(
        self,
        attempt_id: int,
        usage: Usage | None,
        *,
        actual_charge: str | Decimal | None = None,
        error: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Record a response or error; an explicit provider charge can settle missing usage."""
        with self.path.open("a+") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            attempts = {a["attempt_id"]: a for a in self._attempts(self._read(stream))}
            attempt = attempts[attempt_id]
            if attempt["completed"] and attempt.get("charge_usd") is not None:
                raise ValueError("A settled attempt cannot be billed twice")
            estimated = self.prices.charge(attempt["model"], usage) if usage is not None else None
            charge = dollars(actual_charge) if actual_charge is not None else estimated
            self._append(
                stream,
                {
                    "event": "result",
                    "attempt_id": attempt_id,
                    "usage": asdict(usage) if usage is not None else None,
                    "billable_tokens": usage.billable() if usage is not None else None,
                    "charge_usd": str(charge) if charge is not None else None,
                    "charge_basis": "provider"
                    if actual_charge is not None
                    else "frozen_prices"
                    if usage is not None
                    else "unknown",
                    "frozen_price_estimate_usd": str(estimated) if estimated is not None else None,
                    "error": error,
                    "response_metadata": metadata or {},
                },
            )
