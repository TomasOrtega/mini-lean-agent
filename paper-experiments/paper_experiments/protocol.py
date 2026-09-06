"""Predeclared allocation and stopping rules for separate paper campaigns."""

from __future__ import annotations

import hashlib
import json
import random
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any

PROBLEMS = tuple(f"{side}{number}" for side in "AB" for number in range(1, 7))
HARNESSES = ("mini", "axprover")
GEMINI = "gemini-3.8-flash"
OPUS = "claude-opus-4-5"
ASTRA = "gpt-6-astra"
FABLE = "claude-fable-5-1"
MINI_CAMPAIGN_MODELS = {"astra": ASTRA, "fable": FABLE}
MINI_CAMPAIGN_PHASES = {model: campaign for campaign, model in MINI_CAMPAIGN_MODELS.items()}
CAMPAIGNS = ("original", *MINI_CAMPAIGN_MODELS)
MINI_CAMPAIGN_CHECKPOINTS = tuple(map(Decimal, ("2", "5", "10", "20", "50")))
LOW_CHECKPOINTS = (Decimal("0.50"), Decimal("1"), Decimal("2"))
EXTENDED_CHECKPOINTS = tuple(map(Decimal, ("5", "10", "25", "50")))
ANCHOR_CHECKPOINTS = (Decimal("2"), Decimal("5"))


def call_limit(problem: str) -> int:
    return 300 if problem in {"A5", "B6"} else 200


def canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_hash(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


@dataclass(frozen=True)
class Limits:
    dollars: Decimal
    calls: int
    active_seconds: float

    @classmethod
    def for_run(cls, run: dict[str, Any], phase: str) -> Limits:
        calls = call_limit(run["problem"])
        if (
            phase in MINI_CAMPAIGN_MODELS
            and run["model"] == MINI_CAMPAIGN_MODELS[phase]
            and run["harness"] == "mini"
        ):
            if run["problem"] == "smoke":
                return cls(Decimal("2"), 6, 1800)
            return cls(Decimal("50"), calls, 86400)
        if phase == "extended":
            if not run["extendable"]:
                raise ValueError("Only the four predeclared Gemini runs may continue")
            return cls(Decimal("50"), calls, 86400)
        if phase == "anchor" and run["model"] == OPUS:
            return cls(Decimal("5"), calls, 7200)
        if phase == "low" and run["model"] == GEMINI:
            return cls(Decimal("2"), calls, 7200)
        raise ValueError(f"Invalid phase {phase!r} for model {run['model']!r}")

    def to_dict(self) -> dict[str, object]:
        return {**asdict(self), "dollars": str(self.dollars)}


def selected_problems(problems: Sequence[str] | None = None) -> tuple[str, ...]:
    if problems is None:
        return PROBLEMS
    if not problems or any(problem not in PROBLEMS for problem in problems):
        raise ValueError("Select at least one valid benchmark problem")
    if len(set(problems)) != len(problems):
        raise ValueError("Duplicate selected problems are not allowed")
    return tuple(problem for problem in PROBLEMS if problem in problems)


def register_runs(
    seed: int, problems: Sequence[str] | None = None, *, campaign: str = "original"
) -> dict[str, Any]:
    """Shuffle matched harness pairs, interleaving one Opus anchor after each pair."""
    if campaign in MINI_CAMPAIGN_MODELS:
        return register_mini_campaign(seed, problems, campaign=campaign)
    if campaign != "original":
        raise ValueError(f"Unknown campaign: {campaign}")
    selected = selected_problems(problems)
    rng = random.Random(seed)
    blocks = list(selected)
    rng.shuffle(blocks)
    anchor_order = list(selected)
    rng.shuffle(anchor_order)
    runs: list[dict[str, Any]] = []

    def add(model: str, harness: str, problem: str) -> None:
        runs.append(
            {
                "run_id": f"{model}--{harness}--{problem}--r1",
                "model": model,
                "harness": harness,
                "problem": problem,
                "replicate": 1,
                "extendable": model == GEMINI and problem in {"A5", "B6"},
                "order": len(runs),
            }
        )

    for problem, anchor in zip(blocks, anchor_order, strict=True):
        harnesses = list(HARNESSES)
        rng.shuffle(harnesses)
        for harness in harnesses:
            add(GEMINI, harness, problem)
        add(OPUS, "mini", anchor)
    gemini_runs = [run for run in runs if run["model"] == GEMINI]
    low_allocation = sum(Limits.for_run(run, "low").dollars for run in gemini_runs)
    gemini_allocation = sum(
        Limits.for_run(run, "extended" if run["extendable"] else "low").dollars
        for run in gemini_runs
    )
    opus_allocation = sum(
        Limits.for_run(run, "anchor").dollars for run in runs if run["model"] == OPUS
    )
    manifest = {
        "schema_version": 3,
        "seed": seed,
        "scope": {
            "kind": "full_benchmark" if selected == PROBLEMS else "selected_problems",
            "problems": list(selected),
            "benchmark_problem_count": len(PROBLEMS),
        },
        "runs": runs,
        "api_call_limits": {problem: call_limit(problem) for problem in selected},
        "nominal_allocation_usd": {
            "gemini_low": str(low_allocation),
            "gemini": str(gemini_allocation),
            "opus": str(opus_allocation),
            "total": str(gemini_allocation + opus_allocation),
        },
    }
    return {**manifest, "manifest_sha256": content_hash(manifest)}


def register_mini_campaign(
    seed: int, problems: Sequence[str] | None = None, *, campaign: str
) -> dict[str, Any]:
    """Freeze twelve fresh mini trajectories, with no pilot or earlier-run imports."""
    model = MINI_CAMPAIGN_MODELS[campaign]
    if selected_problems(problems) != PROBLEMS:
        raise ValueError(f"The {campaign.title()} campaign requires all twelve benchmark problems")
    order = list(PROBLEMS)
    random.Random(seed).shuffle(order)
    runs = [
        {
            "run_id": f"{model}--mini--{problem}--r1",
            "model": model,
            "harness": "mini",
            "problem": problem,
            "replicate": 1,
            "extendable": False,
            "order": index,
        }
        for index, problem in enumerate(order)
    ]
    manifest = {
        "schema_version": 4,
        "campaign": campaign,
        "seed": seed,
        "scope": {
            "kind": "full_benchmark",
            "problems": list(PROBLEMS),
            "benchmark_problem_count": len(PROBLEMS),
        },
        "runs": runs,
        "api_call_limits": {problem: call_limit(problem) for problem in PROBLEMS},
        "phase": campaign,
        "checkpoints_usd": [str(value) for value in MINI_CAMPAIGN_CHECKPOINTS],
        "limits": {
            problem: Limits.for_run(
                {"model": model, "harness": "mini", "problem": problem}, campaign
            ).to_dict()
            for problem in PROBLEMS
        },
        "workers": 2,
        "nominal_allocation_usd": {campaign: "600", "total": "600"},
    }
    return {**manifest, "manifest_sha256": content_hash(manifest)}


def validate_registration(manifest: dict[str, Any]) -> None:
    """Reject changed assignments, order, caps, or seeds after registration."""
    try:
        expected = register_runs(
            manifest["seed"],
            manifest["scope"]["problems"],
            campaign=manifest.get("campaign", "original"),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("Run registration does not match its frozen protocol") from error
    if manifest != expected:
        raise ValueError("Run registration does not match its seed and frozen protocol")
