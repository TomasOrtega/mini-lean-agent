import json

import pytest

from paper_experiments import cli
from paper_experiments.prepare import file_inventory, sha256, write_json
from paper_experiments.protocol import PROBLEMS, content_hash, register_runs
from paper_experiments.resources import ResourceProfile


def bundle_fixture(tmp_path):
    bundle = tmp_path / "bundle"
    (bundle / "template").mkdir(parents=True)
    (bundle / "template" / "Policy.lean").write_text("trusted")
    (bundle / "dependencies" / "mathlib").mkdir(parents=True)
    (bundle / "dependencies" / "mathlib" / "Mathlib.olean").write_bytes(b"trusted cache")
    write_json(
        bundle / "manifest.json",
        {
            "evaluator_image": "sha256:" + "a" * 64,
            "files": file_inventory(bundle),
            "tasks": [{"id": problem, "declaration": problem.lower()} for problem in PROBLEMS],
        },
    )
    receipt = {
        "kind": "real_comparator_audit",
        "resources": ResourceProfile.load().to_dict(),
        "resources_sha256": ResourceProfile.load().sha256,
        "ok": True,
        "evaluator_image": "sha256:" + "a" * 64,
        "bundle_manifest_sha256": sha256(bundle / "manifest.json"),
        "policy_sha256": sha256(bundle / "template" / "Policy.lean"),
        "verification_timeout_seconds": 600,
        "dependency_cache": cli.cache_reference(bundle),
    }
    return bundle, receipt


def test_execution_error_preserves_erange_trace_without_secrets(tmp_path, monkeypatch):
    import errno
    import sys
    from types import SimpleNamespace

    from paper_experiments import evaluator

    bundle, _ = bundle_fixture(tmp_path)
    run = next(run for run in register_runs(0)["runs"] if run["harness"] == "axprover")
    output = tmp_path / "run"
    secret = "fake-test-openrouter-credential"
    monkeypatch.setenv("OPENROUTER_API_KEY", secret)
    failed_path = str(bundle / "dependencies" / "mathlib" / "Module.trace")

    def failed_retrieval(inputs):
        private_local = "never-archive-this-local"
        try:
            raise ValueError(f"Bearer {secret}")
        except ValueError as cause:
            assert private_local
            raise OSError(errno.ERANGE, f"failed with {secret}", failed_path) from cause

    def create_workspace(bundle, problem, workspace, **kwargs):
        workspace.mkdir(parents=True)
        (workspace / "Target.lean").write_text("example : True := by sorry\n")

    monkeypatch.setattr(cli, "ensure_dependency_cache", lambda *args, **kwargs: {})
    monkeypatch.setattr(cli, "create_workspace", create_workspace)
    monkeypatch.setattr(cli, "validate_retrieval", failed_retrieval)
    monkeypatch.setattr(
        evaluator, "evaluate_candidate", lambda *args, **kwargs: {"verified": False}
    )
    monkeypatch.setitem(
        sys.modules, "paper_experiments.ax_adapter", SimpleNamespace(run_axprover=None)
    )
    cli.run_one(
        run,
        {
            "bundle": str(bundle),
            "resources": ResourceProfile.load().to_dict(),
            "dependency_cache": {},
            "prices": cli.read_json(cli.ROOT / "configs" / "prices.json"),
            "models": {run["model"]: {}},
            "shared_task": "fixture",
            "verification_timeout_seconds": 600,
        },
        output,
        phase="low",
    )
    receipt = cli.read_json(output / "execution-error.json")
    assert receipt["errno"] == errno.ERANGE
    assert receipt["errno_name"] == "ERANGE"
    assert receipt["filename"] == failed_path
    assert receipt["traceback"][-1]["function"] == "failed_retrieval"
    assert receipt["traceback"][-1]["line"] > 0
    assert receipt["exception_chain"][0]["type"] == "ValueError"
    encoded = json.dumps(receipt)
    assert secret not in encoded
    assert "never-archive-this-local" not in encoded
    assert all(set(frame) == {"filename", "function", "line"} for frame in receipt["traceback"])
    state = cli.read_json(output / "controller.json")
    assert state["paused"] is True
    assert state["endpoint"]["stop_reason"] == "infrastructure_failure"
    assert state["endpoint"]["ledger"]["calls"] == 0


def test_registration_pins_audits_settings_and_all_36_runs(tmp_path, monkeypatch):
    bundle, audit = bundle_fixture(tmp_path)
    monkeypatch.setattr(cli, "source_inventory", lambda: {})
    monkeypatch.setattr(cli, "sdk_versions", lambda: {})
    monkeypatch.setattr(cli, "prepare_retrieval", lambda *args: {"path": "unused", "metadata": {}})
    models = cli.read_json(cli.ROOT / "configs" / "models.json")
    smoke = {
        "dependency_cache": cli.cache_reference(bundle),
        "kind": "model_harness_smoke",
        "resources": ResourceProfile.load().to_dict(),
        "resources_sha256": ResourceProfile.load().sha256,
        "ok": True,
        "source_sha256": content_hash({}),
        "models_sha256": content_hash(models),
        "retrieval_settings_sha256": content_hash(cli._retrieval_settings()),
        "bundle_manifest_sha256": sha256(bundle / "manifest.json"),
    }
    write_json(tmp_path / "audit.json", audit)
    write_json(tmp_path / "smoke.json", smoke)
    output = tmp_path / "experiment"
    cli.register_experiment(
        bundle,
        output,
        audit_report=tmp_path / "audit.json",
        smoke_report=tmp_path / "smoke.json",
        seed=2,
    )
    manifest, inputs = cli.experiment_inputs(output)
    assert len(manifest["runs"]) == 36
    assert inputs["shared_task"] == cli.COMMON_TASK
    assert inputs["verification_timeout_seconds"] == 600
    changed = cli.read_json(output / "experiment.json")
    changed["shared_task"] = "new hint"
    write_json(output / "experiment.json", changed)
    with pytest.raises(ValueError, match="inputs have changed"):
        cli.experiment_inputs(output)


def test_registration_rejects_missing_or_mismatched_real_audit(tmp_path):
    bundle, receipt = bundle_fixture(tmp_path)
    receipt["evaluator_image"] = "sha256:" + "b" * 64
    with pytest.raises(ValueError, match="real Comparator"):
        cli.validate_audit(bundle, receipt)
    receipt["evaluator_image"] = "sha256:" + "a" * 64
    receipt["ok"] = False
    with pytest.raises(ValueError, match="real Comparator"):
        cli.validate_audit(bundle, receipt)


def test_registration_rejects_audit_using_different_verification_deadline(tmp_path):
    bundle, receipt = bundle_fixture(tmp_path)
    receipt["verification_timeout_seconds"] = 120
    with pytest.raises(ValueError, match="real Comparator"):
        cli.validate_audit(bundle, receipt)


def test_missing_saved_workspace_cannot_be_replaced(tmp_path):
    bundle, _ = bundle_fixture(tmp_path)
    output = tmp_path / "run"
    output.mkdir()
    (output / "controller.json").write_text("{}")
    run = next(run for run in register_runs(0)["runs"] if run["problem"] == "A1")
    with pytest.raises(ValueError, match="refusing a fresh replacement"):
        cli.run_one(run, {"bundle": str(bundle)}, output, phase="low")
    assert not (output / "workspace").exists()


def test_cli_incomplete_report_does_not_invent_results(tmp_path):
    experiment = tmp_path / "experiment"
    experiment.mkdir()
    write_json(experiment / "runs.json", register_runs(0))
    output = tmp_path / "report"
    cli.main(
        ["report", "--experiment", str(experiment), "--output", str(output), "--allow-incomplete"]
    )
    reports = list(output.glob("*.json"))
    assert reports
    report = json.loads(reports[0].read_text())
    assert report["status"] == "incomplete"


def test_resume_refuses_deleted_ledger_without_recreating_it(tmp_path):
    bundle, _ = bundle_fixture(tmp_path)
    output = tmp_path / "run"
    (output / "workspace").mkdir(parents=True)
    (output / "controller.json").write_text("{}")
    run = next(run for run in register_runs(0)["runs"] if run["problem"] == "A1")
    with pytest.raises(ValueError, match="ledger"):
        cli.run_one(
            run,
            {"bundle": str(bundle), "prices": cli.read_json(cli.ROOT / "configs" / "prices.json")},
            output,
            phase="low",
        )
    assert not (output / "ledger.jsonl").exists()


def test_existing_run_without_controller_cannot_restart(tmp_path):
    bundle, _ = bundle_fixture(tmp_path)
    output = tmp_path / "run"
    output.mkdir()
    (output / "ledger.jsonl").write_text("previous run")
    run = next(run for run in register_runs(0)["runs"] if run["problem"] == "A1")
    with pytest.raises(ValueError, match="controller"):
        cli.run_one(run, {"bundle": str(bundle)}, output, phase="low")


def test_retrieval_metadata_cannot_be_swapped_after_registration(tmp_path, monkeypatch):
    from paper_experiments.retrieval import prepare_native_retrieval

    bundle, _ = bundle_fixture(tmp_path)
    library = bundle / "dependencies" / "mathlib" / "Mathlib.lean"
    library.parent.mkdir(parents=True, exist_ok=True)
    library.write_text("import Mathlib.Algebra\n")
    manifest = cli.read_json(bundle / "manifest.json")
    files = file_inventory(bundle)
    files.pop("manifest.json")
    manifest.update(
        {
            "lean_toolchain": "leanprover/lean4:v4.28.0-rc1",
            "mathlib_revision": cli.read_json(cli.ROOT / "configs" / "putnam2025.json")[
                "mathlib_revision"
            ],
            "files": files,
        }
    )
    write_json(bundle / "manifest.json", manifest)
    receipt = prepare_native_retrieval(bundle, tmp_path / "retrieval")
    inputs = {"bundle": str(bundle), "retrieval": receipt}
    assert cli.validate_retrieval(inputs) == receipt["metadata"]
    write_json(
        tmp_path / "retrieval" / "metadata.json",
        {**receipt["metadata"], "mathlib_revision": "c" * 40},
    )
    with pytest.raises(ValueError, match="LeanSearch"):
        cli.validate_retrieval(inputs)


def test_archived_sources_include_image_and_comparator_wrapper():
    inventory = cli.source_inventory()
    assert "paper-experiments/Dockerfile" in inventory
    assert "paper-experiments/lean/comparator-wrapper" in inventory


def test_resource_profile_must_match_real_audit(tmp_path):
    bundle, receipt = bundle_fixture(tmp_path)
    profile = ResourceProfile.load().to_dict()
    profile["agent_memory_gib"] += 1
    with pytest.raises(ValueError, match="real Comparator"):
        cli.validate_audit(bundle, receipt, profile)


def test_startup_container_receipt_is_cleaned_and_cleanup_is_idempotent(tmp_path, monkeypatch):
    from types import SimpleNamespace

    controller = SimpleNamespace(
        output_dir=tmp_path,
        state={
            "endpoint": {
                "verified": False,
                "resumable": False,
                "stop_reason": "infrastructure_failure",
            },
            "run": {"extendable": False},
            "driver_state": None,
        },
        phase="low",
    )
    write_json(tmp_path / "agent-container.json", {"container_id": "abc123"})
    calls = []

    def remove(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr("paper_experiments.resources.subprocess.run", remove)
    cli.cleanup_finished_container(controller)
    cli.cleanup_finished_container(controller)
    assert calls == [["docker", "rm", "-f", "abc123"]]


def test_failed_container_cleanup_does_not_release_worker_slot(tmp_path, monkeypatch):
    from types import SimpleNamespace

    controller = SimpleNamespace(
        output_dir=tmp_path,
        state={
            "endpoint": {
                "verified": False,
                "resumable": False,
                "stop_reason": "infrastructure_failure",
            },
            "run": {"extendable": False},
            "driver_state": {"container_id": "abc123"},
        },
        phase="low",
    )
    monkeypatch.setattr(
        "paper_experiments.resources.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout=""),
    )
    with pytest.raises(RuntimeError, match="refusing to release"):
        cli.cleanup_finished_container(controller)


def test_pre_start_container_name_receipt_is_cleaned_by_name(tmp_path, monkeypatch):
    from types import SimpleNamespace

    controller = SimpleNamespace(
        output_dir=tmp_path,
        state={
            "endpoint": {
                "verified": False,
                "resumable": False,
                "stop_reason": "infrastructure_failure",
            },
            "run": {"extendable": False},
            "driver_state": None,
        },
        phase="low",
    )
    write_json(tmp_path / "agent-container.json", {"container_name": "mini-lean-starting-ab12"})
    calls = []

    def removed(identifier, *, by_name=False):
        calls.append((identifier, by_name))
        return {"identifier": identifier, "returncode": 0}

    monkeypatch.setattr(cli, "ensure_container_removed", removed)
    cli.cleanup_finished_container(controller)
    cli.cleanup_finished_container(controller)
    assert calls == [("mini-lean-starting-ab12", True)]


def test_cli_passes_repeatable_selected_problems_to_registration(monkeypatch):
    captured = {}
    monkeypatch.setattr(cli, "register_experiment", lambda *args, **kwargs: captured.update(kwargs))
    cli.main(
        [
            "register",
            "--bundle",
            "/bundle",
            "--output",
            "/experiment",
            "--audit-report",
            "/audit",
            "--smoke-report",
            "/smoke",
            "--problem",
            "A1",
            "--problem",
            "B6",
        ]
    )
    assert captured["problems"] == ["A1", "B6"]


def test_selected_scope_is_bound_to_frozen_inputs_for_run_and_report(tmp_path, monkeypatch):
    bundle, audit = bundle_fixture(tmp_path)
    monkeypatch.setattr(cli, "source_inventory", lambda: {})
    monkeypatch.setattr(cli, "sdk_versions", lambda: {})
    monkeypatch.setattr(cli, "prepare_retrieval", lambda *args: {"metadata": {}})
    models = cli.read_json(cli.ROOT / "configs" / "models.json")
    write_json(tmp_path / "audit.json", audit)
    write_json(
        tmp_path / "smoke.json",
        {
            "kind": "model_harness_smoke",
            "dependency_cache": cli.cache_reference(bundle),
            "ok": True,
            "resources": ResourceProfile.load().to_dict(),
            "resources_sha256": ResourceProfile.load().sha256,
            "source_sha256": content_hash({}),
            "models_sha256": content_hash(models),
            "retrieval_settings_sha256": content_hash(cli._retrieval_settings()),
            "bundle_manifest_sha256": sha256(bundle / "manifest.json"),
        },
    )
    output = tmp_path / "experiment"
    cli.register_experiment(
        bundle,
        output,
        audit_report=tmp_path / "audit.json",
        smoke_report=tmp_path / "smoke.json",
        seed=2025,
        problems=["A1"],
    )
    manifest, inputs = cli.experiment_inputs(output)
    assert len(manifest["runs"]) == 3
    assert inputs["registration_sha256"] == content_hash(manifest)
    dispatched = []
    monkeypatch.setattr(
        "paper_experiments.scheduler.run_queue",
        lambda experiment, runs, inputs, phase: dispatched.extend(runs),
    )
    cli.run_experiment(output, "all")
    assert len(dispatched) == 3
    assert {run["problem"] for run in dispatched} == {"A1"}
    write_json(output / "runs.json", register_runs(2025, problems=["A2"]))
    with pytest.raises(ValueError, match=r"registration.*changed"):
        cli.experiment_inputs(output)
    with pytest.raises(ValueError, match=r"registration.*changed"):
        cli.main(
            [
                "report",
                "--experiment",
                str(output),
                "--output",
                str(tmp_path / "report"),
                "--allow-incomplete",
            ]
        )


def mini_campaign_fixture(tmp_path, monkeypatch, campaign="astra"):
    from paper_experiments.protocol import MINI_CAMPAIGN_MODELS

    model = MINI_CAMPAIGN_MODELS[campaign]

    bundle, audit = bundle_fixture(tmp_path)
    profile = ResourceProfile.from_dict(ResourceProfile.load().to_dict() | {"workers": 2})
    audit.update(resources=profile.to_dict(), resources_sha256=profile.sha256)
    write_json(tmp_path / "audit.json", audit)
    write_json(tmp_path / "resources.json", profile.to_dict())
    write_json(tmp_path / "models.json", {model: {"mini_model": f"test/{campaign}"}})
    write_json(
        tmp_path / "prices.json",
        {
            "as_of": "2026-09-05",
            "source": "fixture",
            "models": {
                model: dict.fromkeys(
                    ("input", "output", "reasoning", "cache_read", "cache_write"), "1"
                )
            },
        },
    )
    monkeypatch.setattr(cli, "source_inventory", lambda: {})
    monkeypatch.setattr(cli, "sdk_versions", lambda: {})
    monkeypatch.setattr(
        cli,
        "prepare_retrieval",
        lambda *args: pytest.fail("mini-only campaign contacted LeanSearch"),
    )
    monkeypatch.setattr(
        cli,
        "_retrieval_settings",
        lambda: pytest.fail("mini-only campaign read LeanSearch settings"),
    )
    return bundle, {
        "audit_report": tmp_path / "audit.json",
        "resources_path": tmp_path / "resources.json",
        "models_path": tmp_path / "models.json",
        "prices_path": tmp_path / "prices.json",
        "campaign": campaign,
    }


@pytest.mark.parametrize("campaign", ["astra", "fable"])
def test_mini_campaign_smoke_and_registration_use_exact_configs_without_leansearch(
    tmp_path, monkeypatch, campaign
):
    from paper_experiments.protocol import MINI_CAMPAIGN_MODELS

    model = MINI_CAMPAIGN_MODELS[campaign]

    bundle, settings = mini_campaign_fixture(tmp_path, monkeypatch, campaign)
    dispatched = []

    def fake_run(run, inputs, output, *, phase):
        challenge = cli.Path(inputs["bundle"]) / "tasks" / "smoke" / "Challenge.lean"
        assert "smoke_identity" in challenge.read_text()
        assert run["problem"] == "smoke"
        assert inputs["retrieval"] is None
        assert inputs["models"] == cli.read_json(settings["models_path"])
        assert inputs["prices"] == cli.read_json(settings["prices_path"])
        dispatched.append((run["model"], run["harness"], phase))
        write_json(
            output / "controller.json", {"endpoint": {"verified": True}, "ledger": {"calls": 1}}
        )

    monkeypatch.setattr(cli, "run_one", fake_run)
    smoke_output = tmp_path / "smoke"
    cli.smoke_models(bundle, smoke_output, **settings)
    assert dispatched == [(model, "mini", campaign)]
    smoke = cli.read_json(smoke_output / "smoke-report.json")
    assert smoke["campaign"] == campaign
    assert smoke["retrieval_settings_sha256"] == content_hash(None)
    output = tmp_path / "experiment"
    cli.register_experiment(
        bundle, output, smoke_report=smoke_output / "smoke-report.json", seed=2025, **settings
    )
    manifest, inputs = cli.experiment_inputs(output)
    assert len(manifest["runs"]) == 12
    assert inputs["retrieval"] is None
    assert inputs["resources"]["workers"] == 2
    assert inputs["prices"] == cli.read_json(settings["prices_path"])
    assert inputs["models"] == cli.read_json(settings["models_path"])
    assert not (output / "retrieval").exists()


@pytest.mark.parametrize("campaign", ["astra", "fable"])
def test_mini_campaign_registration_requires_two_workers_before_smoke_or_dispatch(
    tmp_path, monkeypatch, campaign
):
    bundle, settings = mini_campaign_fixture(tmp_path, monkeypatch, campaign)
    write_json(settings["resources_path"], ResourceProfile.load().to_dict())
    with pytest.raises(ValueError, match="exactly two workers"):
        cli.register_experiment(
            bundle, tmp_path / "experiment", smoke_report=tmp_path / "unused", seed=2025, **settings
        )
    assert not (tmp_path / "experiment").exists()


@pytest.mark.parametrize("campaign", ["astra", "fable"])
def test_cli_passes_mini_campaign_and_config_paths(monkeypatch, campaign):
    from pathlib import Path

    captured = {}
    monkeypatch.setattr(cli, "register_experiment", lambda *args, **kwargs: captured.update(kwargs))
    cli.main(
        [
            "register",
            "--bundle",
            "/bundle",
            "--output",
            "/experiment",
            "--audit-report",
            "/audit",
            "--smoke-report",
            "/smoke",
            "--campaign",
            campaign,
            "--models",
            f"/{campaign}-models.json",
            "--prices",
            f"/{campaign}-prices.json",
            "--resources",
            "/two-workers.json",
        ]
    )
    assert captured["campaign"] == campaign
    assert captured["models_path"] == Path(f"/{campaign}-models.json")
    assert captured["prices_path"] == Path(f"/{campaign}-prices.json")
    assert captured["resources_path"] == Path("/two-workers.json")
    assert captured["problems"] is None


def test_fable_registration_rejects_astra_smoke_receipt(tmp_path, monkeypatch):
    bundle, settings = mini_campaign_fixture(tmp_path, monkeypatch, "fable")
    smoke = tmp_path / "astra-smoke.json"
    write_json(smoke, {"campaign": "astra", "ok": True})
    output = tmp_path / "experiment"
    with pytest.raises(ValueError, match="Fable registration requires"):
        cli.register_experiment(bundle, output, smoke_report=smoke, seed=2025, **settings)
    assert not output.exists()
