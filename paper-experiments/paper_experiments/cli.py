"""Prepare, audit, register, execute, and report the frozen paper experiment."""

from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import json
import re
import shutil
from pathlib import Path
from typing import Any

from .cache import ensure_dependency_cache
from .cache import reference as cache_reference
from .controller import RunController, atomic_json
from .diagnostics import error_details
from .evaluator import VERIFICATION_TIMEOUT_SECONDS
from .ledger import Ledger, PriceTable
from .models import mini_model_kwargs
from .prepare import ROOT, create_workspace, load_manifest, prepare_manifest, sha256, task_record
from .protocol import (
    CAMPAIGNS,
    GEMINI,
    MINI_CAMPAIGN_MODELS,
    MINI_CAMPAIGN_PHASES,
    OPUS,
    PROBLEMS,
    content_hash,
    register_runs,
    validate_registration,
)
from .resources import (
    DEFAULT_RESOURCES,
    ContainerCleanupError,
    ResourceProfile,
    docker_execution_lock,
    ensure_container_removed,
    preflight,
    resolve_resources,
)

COMMON_TASK = """Complete the proof of the designated theorem in Target.lean.
The registered project uses Lean 4.28.0-rc1 and Mathlib v4.28.0-rc1.
Preserve the supplied statement, definitions, imports, and project configuration.
Keep the supplied source prefix before the target theorem byte-identical. You may add proved
helper theorems after that prefix, using plain `theorem` commands (not `lemma`, declaration
attributes, or modifiers), and create scratch files while working. Submit Target.lean with
no sorry, admit, additional axioms, or metaprogramming commands. The final artifact is checked
independently in the original pinned environment. Use only the supplied tools; external models and open-web browsing are forbidden."""


def read_json(path: Path) -> Any:
    return json.loads(path.read_text())


def source_inventory() -> dict[str, str]:
    paths = [*ROOT.glob("*.py"), *ROOT.glob("*.toml"), *ROOT.glob("*.lock"), ROOT / "Dockerfile"]
    for directory in (
        ROOT / "paper_experiments",
        ROOT / "configs",
        ROOT / "lean",
        ROOT / "tests",
        ROOT.parent / "src",
    ):
        paths.extend(
            p
            for p in directory.rglob("*")
            if p.is_file()
            and "__pycache__" not in p.parts
            and (directory.name == "lean" or p.suffix in {".py", ".yaml", ".json", ".lean"})
        )
    return {str(path.relative_to(ROOT.parent)): sha256(path) for path in sorted(paths)}


def sdk_versions() -> dict[str, str]:
    return {
        name: importlib.metadata.version(name)
        for name in (
            "mini-lean-agent",
            "mini-swe-agent",
            "ax-prover",
            "litellm",
            "google-genai",
            "langchain-google-genai",
            "langchain-openrouter",
            "openrouter",
            "openai",
            "langchain-openai",
            "langgraph",
            "langgraph-checkpoint-sqlite",
            "lean-interact",
        )
    }


def validate_audit(
    bundle: Path, receipt: dict[str, Any], resources: dict[str, Any] | None = None
) -> None:
    profile = resolve_resources(resources)
    manifest = load_manifest(bundle)
    if not (
        receipt.get("kind") == "real_comparator_audit"
        and receipt.get("ok") is True
        and receipt.get("evaluator_image") == manifest["evaluator_image"]
        and receipt.get("bundle_manifest_sha256") == sha256(bundle / "manifest.json")
        and receipt.get("policy_sha256") == sha256(bundle / "template" / "Policy.lean")
        and receipt.get("resources") == profile.to_dict()
        and receipt.get("resources_sha256") == profile.sha256
        and receipt.get("dependency_cache") == cache_reference(bundle, manifest)
        and receipt.get("verification_timeout_seconds") == VERIFICATION_TIMEOUT_SECONDS
    ):
        raise ValueError(
            "A successful real Comparator audit for this exact bundle/image is required"
        )


def _retrieval_settings() -> dict[str, Any]:
    from .retrieval import registered_settings

    return registered_settings()


def prepare_retrieval(bundle: Path, archive: Path) -> dict[str, Any]:
    from .retrieval import prepare_native_retrieval

    return prepare_native_retrieval(bundle, archive)


def validate_retrieval(inputs: dict[str, Any]) -> dict[str, Any]:
    from .retrieval import validate_registered_retrieval

    return validate_registered_retrieval(inputs)


def register_experiment(
    bundle: Path,
    output: Path,
    *,
    audit_report: Path,
    smoke_report: Path,
    seed: int,
    models_path: Path = ROOT / "configs" / "models.json",
    prices_path: Path = ROOT / "configs" / "prices.json",
    resources_path: Path = DEFAULT_RESOURCES,
    problems: list[str] | None = None,
    campaign: str = "original",
) -> None:
    bundle, output = bundle.resolve(), output.resolve()
    registration = register_runs(seed, problems, campaign=campaign)
    audit = read_json(audit_report)
    profile = ResourceProfile.load(resources_path)
    if campaign in MINI_CAMPAIGN_MODELS and profile.workers != 2:
        raise ValueError(f"The {campaign.title()} campaign requires exactly two workers")
    validate_audit(bundle, audit, profile.to_dict())
    available = {task["id"] for task in load_manifest(bundle)["tasks"]}
    if not set(registration["scope"]["problems"]) <= available:
        raise ValueError("Selected problems are missing from the trusted bundle")
    models, prices, smoke = read_json(models_path), read_json(prices_path), read_json(smoke_report)
    PriceTable.from_dict(prices)
    if campaign in MINI_CAMPAIGN_MODELS and not (
        MINI_CAMPAIGN_MODELS[campaign] in models
        and MINI_CAMPAIGN_MODELS[campaign] in prices["models"]
        and smoke.get("campaign") == campaign
        and smoke.get("prices_sha256") == content_hash(prices)
        and [
            (case.get("model"), case.get("harness"), case.get("verified"))
            for case in smoke.get("cases", [])
        ]
        == [(MINI_CAMPAIGN_MODELS[campaign], "mini", True)]
    ):
        raise ValueError(
            f"{campaign.title()} registration requires its matching model, prices, and mini smoke case"
        )
    fingerprint = content_hash(source_inventory())
    if not (
        smoke.get("kind") == "model_harness_smoke"
        and smoke.get("ok") is True
        and smoke.get("source_sha256") == fingerprint
        and smoke.get("models_sha256") == content_hash(models)
        and smoke.get("bundle_manifest_sha256") == sha256(bundle / "manifest.json")
        and smoke.get("resources") == profile.to_dict()
        and smoke.get("resources_sha256") == profile.sha256
        and smoke.get("dependency_cache") == cache_reference(bundle)
    ):
        raise ValueError(
            "Run the non-Putnam model/harness smoke test with these exact settings before registration"
        )
    retrieval_settings = None if campaign in MINI_CAMPAIGN_MODELS else _retrieval_settings()
    if smoke.get("retrieval_settings_sha256") != content_hash(retrieval_settings):
        raise ValueError("LeanSearch settings changed since the model/harness smoke test")
    if output.exists():
        raise FileExistsError("Registration must use a new directory")
    output.mkdir(parents=True)
    atomic_json(output / "runs.json", registration)
    inputs = {
        "schema_version": 2,
        "registration_sha256": content_hash(registration),
        "bundle": str(bundle),
        "bundle_manifest_sha256": sha256(bundle / "manifest.json"),
        "models": models,
        "prices": prices,
        "resources": profile.to_dict(),
        "resources_sha256": profile.sha256,
        "dependency_cache": cache_reference(bundle),
        "scheduler_policy": "spawn workers; registered FIFO with existing containers first; phase all continues designated runs in the same slot",
        "shared_task": COMMON_TASK,
        "source_files": source_inventory(),
        "source_sha256": fingerprint,
        "sdk_versions": sdk_versions(),
        "audit": audit,
        "smoke": smoke,
        "retrieval": None
        if campaign in MINI_CAMPAIGN_MODELS
        else prepare_retrieval(bundle, output / "retrieval"),
        "verification_timeout_seconds": VERIFICATION_TIMEOUT_SECONDS,
    }
    atomic_json(output / "experiment.json", inputs)
    atomic_json(output / "inputs.sha256.json", {"sha256": content_hash(inputs)})
    archive = output / "source"
    for relative in inputs["source_files"]:
        source = ROOT.parent / relative
        dest = archive / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dest)


def experiment_inputs(experiment: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    manifest, inputs = (
        read_json(experiment / "runs.json"),
        read_json(experiment / "experiment.json"),
    )
    validate_registration(manifest)
    if content_hash(inputs) != read_json(experiment / "inputs.sha256.json")["sha256"]:
        raise ValueError("Registered inputs have changed")
    if content_hash(manifest) != inputs.get("registration_sha256"):
        raise ValueError("Frozen run registration has changed")
    if inputs["source_files"] != source_inventory() or inputs["sdk_versions"] != sdk_versions():
        raise ValueError("Experiment code or SDK versions changed after registration")
    if sha256(Path(inputs["bundle"]) / "manifest.json") != inputs["bundle_manifest_sha256"]:
        raise ValueError("Benchmark manifest changed after registration")
    if inputs.get("dependency_cache") != cache_reference(Path(inputs["bundle"])):
        raise ValueError("Frozen dependency cache differs from the registered bundle")
    return manifest, inputs


def run_one(run: dict[str, Any], inputs: dict[str, Any], output: Path, *, phase: str) -> None:
    from .evaluator import evaluate_candidate

    registered_phase = MINI_CAMPAIGN_PHASES.get(run["model"])
    if registered_phase and phase not in {"all", registered_phase}:
        raise ValueError(f"{registered_phase.title()} runs require phase all or {registered_phase}")
    if phase in MINI_CAMPAIGN_MODELS and phase != registered_phase:
        raise ValueError(f"The {phase} phase requires a matching campaign registration")
    bundle = Path(inputs["bundle"])
    task = task_record(load_manifest(bundle), run["problem"])
    workspace = output / "workspace"
    if output.exists() and any(output.iterdir()) and not (output / "controller.json").exists():
        raise ValueError("Existing run is missing its controller; refusing a fresh replacement")
    if not workspace.exists() and (output / "controller.json").exists():
        raise ValueError("Saved workspace is missing; refusing a fresh replacement")
    if (output / "controller.json").exists() and not (output / "ledger.jsonl").is_file():
        raise ValueError("Saved run ledger is missing; refusing to reset accounting")
    dependency_cache = ensure_dependency_cache(bundle, resources=inputs["resources"])
    if dependency_cache != inputs["dependency_cache"]:
        raise ValueError("Frozen dependency cache differs from the registered bundle")
    if not workspace.exists():
        create_workspace(bundle, run["problem"], workspace, dependency_cache=dependency_cache)
    ledger = Ledger(output / "ledger.jsonl", PriceTable.from_dict(inputs["prices"]))
    controller = RunController(
        run,
        output,
        ledger,
        lambda candidate: evaluate_candidate(
            bundle,
            run["problem"],
            candidate,
            timeout_seconds=inputs["verification_timeout_seconds"],
            resources=inputs["resources"],
            dependency_cache=dependency_cache,
        ),
    )
    resume_state = controller.state["driver_state"]
    if phase == "extended":
        if not run["extendable"]:
            return
        low = controller.state["low_endpoint"]
        if low is None:
            raise ValueError(f"Run {run['run_id']} has no frozen low-budget endpoint")
        if (
            low["verified"]
            or not low.get("resumable", True)
            or low["stop_reason"] not in {"cost", "call_count", "elapsed_time"}
        ):
            return
        if controller.phase == "low":
            resume_state = controller.resume_extended()
    elif controller.phase == "extended":
        return
    if controller.state["paused"]:
        cleanup_finished_container(controller)
        return
    settings = inputs["models"][run["model"]]
    common = {
        "workspace": workspace,
        "target": Path("Target.lean"),
        "declaration": task["declaration"],
        "task": inputs["shared_task"],
        "image": load_manifest(bundle, verify=False)["evaluator_image"],
        "resume_state": resume_state,
        "resources": inputs["resources"],
        "dependency_cache": dependency_cache,
    }
    try:
        if run["harness"] == "mini":
            from .mini_adapter import run_mini

            run_mini(
                controller,
                **common,
                retry_attempts=settings["max_attempts"],
                model_config={
                    "model_name": settings["mini_model"],
                    "model_kwargs": mini_model_kwargs(settings),
                    "cost_tracking": "ignore_errors",
                    "set_cache_control": None,
                },
            )
        else:
            from .ax_adapter import run_axprover

            retrieval = validate_retrieval(inputs)
            asyncio.run(run_axprover(controller, **common, settings=settings, retrieval=retrieval))
    except ContainerCleanupError:
        raise
    except Exception as error:
        atomic_json(output / "execution-error.json", error_details(error))
        if not controller.state["paused"]:
            controller.after_boundary(
                workspace / "Target.lean",
                controller.state["driver_state"] or {"resumable": False},
                stop_reason="infrastructure_failure",
                boundary="execution_error",
            )
    finally:
        cleanup_finished_container(controller)


def cleanup_finished_container(controller: RunController) -> None:
    endpoint = controller.state["endpoint"]
    if endpoint is None:
        return
    if (
        controller.phase == "low"
        and controller.state["run"]["extendable"]
        and endpoint.get("resumable", False)
        and not endpoint["verified"]
        and endpoint["stop_reason"] in {"cost", "call_count", "elapsed_time"}
    ):
        return
    driver = controller.state["driver_state"] or {}
    container = driver.get("container_id") or driver.get("sandbox", {}).get("container_id")
    by_name = False
    container_record = controller.output_dir / "agent-container.json"
    if not container and container_record.exists():
        recorded = read_json(container_record)
        container = recorded.get("container_id") or recorded.get("container_name")
        by_name = not recorded.get("container_id")
    if container and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", container):
        cleanup_path = controller.output_dir / "container-cleanup.json"
        if cleanup_path.exists():
            previous = read_json(cleanup_path)
            identifier = previous.get("identifier", previous.get("container_id"))
            if identifier == container and previous.get("returncode") == 0:
                return
        receipt = ensure_container_removed(container, by_name=by_name)
        key = "container_name" if by_name else "container_id"
        atomic_json(cleanup_path, {**receipt, key: container, "by_name": by_name})


def run_experiment(experiment: Path, phase: str, run_id: str | None = None) -> None:
    manifest, inputs = experiment_inputs(experiment)
    runs = manifest["runs"]
    if run_id:
        selected = [run for run in runs if run["run_id"] == run_id]
        if not selected:
            raise ValueError(f"Unregistered run: {run_id}")
        if phase in {"low", "all", *MINI_CAMPAIGN_MODELS}:
            for earlier in runs[: selected[0]["order"]]:
                state_path = experiment / "runs" / earlier["run_id"] / "controller.json"
                if not state_path.exists() or not read_json(state_path)["paused"]:
                    raise ValueError(
                        "Start runs in registered order; complete the earlier run first"
                    )
        runs = selected
    from .scheduler import run_queue

    run_queue(experiment, runs, inputs, phase)


def smoke_models(
    bundle: Path,
    output: Path,
    audit_report: Path,
    *,
    resources_path: Path = DEFAULT_RESOURCES,
    models_path: Path = ROOT / "configs" / "models.json",
    prices_path: Path = ROOT / "configs" / "prices.json",
    campaign: str = "original",
) -> None:
    from .prepare import file_inventory, write_json

    if campaign not in CAMPAIGNS:
        raise ValueError(f"Unknown campaign: {campaign}")
    bundle = bundle.resolve()
    profile = ResourceProfile.load(resources_path)
    validate_audit(bundle, read_json(audit_report), profile.to_dict())
    if output.exists():
        raise FileExistsError("Smoke tests must use a new output directory")
    smoke_bundle = output / "bundle"
    smoke_bundle.mkdir(parents=True)
    original = load_manifest(bundle)
    shutil.copytree(bundle / "template", smoke_bundle / "template")
    shutil.copytree(bundle / "dependencies", smoke_bundle / "dependencies")
    task_dir = smoke_bundle / "tasks" / "smoke"
    task_dir.mkdir(parents=True)
    prefix = "import Mathlib\n\n"
    challenge = prefix + "theorem smoke_identity (n : Nat) : n = n := by\n  sorry\n"
    for name in ("Challenge.lean", "Target.lean"):
        (task_dir / name).write_text(challenge)
    (task_dir / "Prefix.lean").write_text(prefix)
    smoke_manifest = {
        **original,
        "tasks": [
            {
                "id": "smoke",
                "declaration": "smoke_identity",
                "prefix_bytes": len(prefix.encode()),
                "sha256": sha256(task_dir / "Challenge.lean"),
            }
        ],
        "files": file_inventory(smoke_bundle),
    }
    write_json(smoke_bundle / "manifest.json", smoke_manifest)
    inputs = {
        "bundle": str(smoke_bundle),
        "models": read_json(models_path),
        "prices": read_json(prices_path),
        "dependency_cache": cache_reference(smoke_bundle),
        "resources": profile.to_dict(),
        "shared_task": COMMON_TASK,
        "verification_timeout_seconds": VERIFICATION_TIMEOUT_SECONDS,
        "retrieval": None
        if campaign in MINI_CAMPAIGN_MODELS
        else prepare_retrieval(smoke_bundle, output / "retrieval"),
    }
    cases = []
    pairs = (
        [(MINI_CAMPAIGN_MODELS[campaign], "mini")]
        if campaign in MINI_CAMPAIGN_MODELS
        else [(GEMINI, "mini"), (GEMINI, "axprover"), (OPUS, "mini")]
    )
    for model, harness in pairs:
        run = {
            "run_id": f"smoke--{model}--{harness}",
            "model": model,
            "harness": harness,
            "problem": "smoke",
            "replicate": 1,
            "extendable": False,
        }
        run_one(
            run,
            inputs,
            output / run["run_id"],
            phase=campaign if campaign in MINI_CAMPAIGN_MODELS else "low",
        )
        state = read_json(output / run["run_id"] / "controller.json")
        cases.append(
            {
                "model": model,
                "harness": harness,
                "verified": state["endpoint"]["verified"],
                "ledger": state["ledger"],
            }
        )
    atomic_json(
        output / "smoke-report.json",
        {
            "kind": "model_harness_smoke",
            "campaign": campaign,
            "prices_sha256": content_hash(inputs["prices"]),
            "dependency_cache": cache_reference(bundle),
            "resources": profile.to_dict(),
            "resources_sha256": profile.sha256,
            "ok": all(case["verified"] for case in cases),
            "source_sha256": content_hash(source_inventory()),
            "models_sha256": content_hash(inputs["models"]),
            "retrieval_settings_sha256": content_hash(
                None if campaign in MINI_CAMPAIGN_MODELS else _retrieval_settings()
            ),
            "bundle_manifest_sha256": sha256(bundle / "manifest.json"),
            "cases": cases,
        },
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    project = commands.add_parser(
        "project", help="Create the pinned Lean/Mathlib execution project"
    )
    project.add_argument("--source", type=Path, required=True)
    project.add_argument("--output", type=Path, required=True)
    prepare = commands.add_parser(
        "prepare", help="Pin trusted challenges and prebuilt dependencies"
    )
    prepare.add_argument(
        "--source", type=Path, required=True, help="Pinned Numina leanproblems directory"
    )
    prepare.add_argument("--bundle", type=Path, required=True)
    prepare.add_argument("--image", required=True, help="Built Docker image sha256 digest")
    prepare.add_argument("--mini-revision", required=True)
    prepare.add_argument("--evaluator-revision", required=True)
    prepare.add_argument("--dependencies", type=Path)
    audit = commands.add_parser("audit", help="Run real Comparator checks on non-Putnam theorems")
    audit.add_argument("--bundle", type=Path, required=True)
    audit.add_argument("--output", type=Path, required=True)
    audit.add_argument("--resources", type=Path, default=DEFAULT_RESOURCES)
    smoke = commands.add_parser("smoke", help="Run paid, non-Putnam model/harness smoke tests")
    smoke.add_argument("--bundle", type=Path, required=True)
    smoke.add_argument("--output", type=Path, required=True)
    smoke.add_argument("--audit-report", type=Path, required=True)
    smoke.add_argument("--resources", type=Path, default=DEFAULT_RESOURCES)
    smoke.add_argument("--campaign", choices=CAMPAIGNS, default="original")
    smoke.add_argument("--models", type=Path, default=ROOT / "configs" / "models.json")
    smoke.add_argument("--prices", type=Path, default=ROOT / "configs" / "prices.json")
    register = commands.add_parser("register", help="Freeze benchmark or selected-problem runs")
    register.add_argument("--bundle", type=Path, required=True)
    register.add_argument("--output", type=Path, required=True)
    register.add_argument("--audit-report", type=Path, required=True)
    register.add_argument("--smoke-report", type=Path, required=True)
    register.add_argument("--seed", type=int, default=2025)
    register.add_argument("--resources", type=Path, default=DEFAULT_RESOURCES)
    register.add_argument("--campaign", choices=CAMPAIGNS, default="original")
    register.add_argument("--models", type=Path, default=ROOT / "configs" / "models.json")
    register.add_argument("--prices", type=Path, default=ROOT / "configs" / "prices.json")
    register.add_argument(
        "--problem",
        action="append",
        choices=PROBLEMS,
        help="Register only these problems (repeatable; default: all twelve)",
    )
    run = commands.add_parser(
        "run", help="Execute registered runs or their predeclared continuations"
    )
    run.add_argument("--experiment", type=Path, required=True)
    run.add_argument(
        "--phase", choices=("all", "low", "extended", *MINI_CAMPAIGN_MODELS), default="all"
    )
    run.add_argument("--run-id")
    report = commands.add_parser("report", help="Export JSON, CSV, and blue LaTeX result tables")
    report.add_argument("--experiment", type=Path, required=True)
    report.add_argument("--output", type=Path, required=True)
    report.add_argument("--allow-incomplete", action="store_true")
    report.add_argument("--annotations", type=Path)
    annotate = commands.add_parser(
        "annotations", help="Create the fixed-rubric manual trace annotation CSV"
    )
    annotate.add_argument("--experiment", type=Path, required=True)
    annotate.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "project":
        from .prepare import write_project_template

        write_project_template(args.source, args.output)
    elif args.command == "prepare":
        prepare_manifest(
            args.source,
            args.bundle,
            image=args.image,
            dependencies=args.dependencies,
            harness_revisions={
                "mini": args.mini_revision,
                "ax": "c8e845533d386e198b32fc3f00841fc3e77094c2",
                "evaluator": args.evaluator_revision,
            },
        )
    elif args.command == "audit":
        from .evaluator import audit_evaluator

        profile = ResourceProfile.load(args.resources)
        with docker_execution_lock():
            admission = preflight(profile, workers=0, audit_only=True)
            receipt = audit_evaluator(args.bundle, output=args.output, resources=profile.to_dict())
            receipt["admission"] = admission
            atomic_json(args.output, receipt)
        if not receipt["ok"]:
            raise SystemExit("Real Comparator audit failed; inspect the saved report")
    elif args.command == "smoke":
        profile = ResourceProfile.load(args.resources)
        with docker_execution_lock():
            admission = preflight(profile, workers=1)
            smoke_models(
                args.bundle,
                args.output,
                args.audit_report,
                resources_path=args.resources,
                models_path=args.models,
                prices_path=args.prices,
                campaign=args.campaign,
            )
            receipt = read_json(args.output / "smoke-report.json")
            receipt["admission"] = admission
            atomic_json(args.output / "smoke-report.json", receipt)
    elif args.command == "register":
        register_experiment(
            args.bundle,
            args.output,
            audit_report=args.audit_report,
            smoke_report=args.smoke_report,
            seed=args.seed,
            resources_path=args.resources,
            problems=args.problem,
            campaign=args.campaign,
            models_path=args.models,
            prices_path=args.prices,
        )
    elif args.command == "run":
        run_experiment(args.experiment, args.phase, args.run_id)
    else:
        from .reporting import build_report, write_annotation_template, write_report

        manifest = read_json(args.experiment / "runs.json")
        if args.command == "annotations":
            write_annotation_template(manifest, args.output)
        else:
            result = build_report(
                manifest,
                args.experiment / "runs",
                allow_incomplete=args.allow_incomplete,
                annotations_path=args.annotations,
            )
            inputs_path = args.experiment / "experiment.json"
            if inputs_path.exists():
                inputs = read_json(inputs_path)
                if (
                    content_hash(inputs)
                    != read_json(args.experiment / "inputs.sha256.json")["sha256"]
                ):
                    raise ValueError("Registered report inputs have changed")
                if content_hash(manifest) != inputs.get("registration_sha256"):
                    raise ValueError("Frozen run registration has changed")
                result["provenance"] = inputs
                result["provenance"]["experiment_sha256"] = content_hash(inputs)
            elif args.allow_incomplete:
                result["provenance"] = {"status": "registration unavailable"}
            else:
                raise ValueError(
                    "Reporting completed results requires the frozen experiment inputs"
                )
            write_report(result, args.output)
