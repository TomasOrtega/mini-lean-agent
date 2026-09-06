"""Preparation and Docker orchestration tests; these do not prove Lean soundness."""

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from paper_experiments import cache, evaluator, prepare


@pytest.fixture
def prepared_bundle(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    challenge = (
        b"import Mathlib\ndef preserved : Nat := 42\ntheorem task : preserved = 42 := by sorry\n"
    )
    (source / "Task.lean").write_bytes(challenge)
    (source / "lean-toolchain").write_text("leanprover/lean4:v4.26.0\n")
    (source / "lakefile.toml").write_text(
        'name = "leanproblems"\n[[lean_lib]]\nname = "Leanproblems"\n'
    )
    (source / "lake-manifest.json").write_text(
        json.dumps({"packages": [{"type": "git", "name": "mathlib", "rev": "a" * 40}]})
    )
    (source / "solution.lean").write_text("DO NOT COPY")
    (source / ".git").mkdir()
    (source / ".git" / "history").write_text("DO NOT COPY")
    dependency = source / ".lake" / "packages" / "mathlib"
    (dependency / "Mathlib").mkdir(parents=True)
    (dependency / "Mathlib" / "True.lean").write_text("theorem ordinary : True := True.intro\n")
    (dependency / "lean-toolchain").write_text("leanprover/lean4:v4.26.0\n")
    (dependency / "Scratch.lean").write_text("PRIVATE UNTRACKED SOLUTION")
    (dependency / ".git").mkdir()
    (dependency / ".git" / "history").write_text("HISTORY")
    build = dependency / ".lake" / "build" / "lib" / "lean" / "Mathlib"
    build.mkdir(parents=True)
    (build / "True.olean").write_bytes(b"trusted compiled cache")
    (build / "True.ir").write_bytes(b"trusted interpreter code")
    (build / "Scratch.olean").write_bytes(b"DO NOT COPY")
    ir = dependency / ".lake" / "build" / "ir" / "Mathlib"
    ir.mkdir(parents=True)
    (ir / "True.c").write_bytes(b"trusted generated C")
    (ir / "True.c.hash").write_bytes(b"trusted C hash")
    (ir / "Scratch.c").write_bytes(b"DO NOT COPY")
    monkeypatch.setattr(prepare, "git_revision", lambda _: "a" * 40)

    def git_output(command, **_):
        if "ls-files" in command:
            return "Mathlib/True.lean\0lean-toolchain\0"
        if "status" in command:
            return ""
        raise AssertionError(command)

    monkeypatch.setattr(prepare.subprocess, "check_output", git_output)
    spec = {
        "source": {"revision": "a" * 40, "url": "https://example.test/trusted"},
        "source_files": {
            name: prepare.sha256(source / name)
            for name in ("Task.lean", "lean-toolchain", "lakefile.toml", "lake-manifest.json")
        },
        "lean_toolchain": "leanprover/lean4:v4.26.0",
        "mathlib_revision": "a" * 40,
        "comparator_revision": "b" * 40,
        "exporter_revision": "c" * 40,
        "landrun_revision": "d" * 40,
        "lean_interact_version": "0.11.5",
        "repl_revision": "e" * 40,
        "tasks": [
            {
                "id": "A1",
                "source": "Task.lean",
                "declaration": "task",
                "sha256": hashlib.sha256(challenge).hexdigest(),
                "prefix_bytes": challenge.index(b"theorem task"),
            }
        ],
    }
    specification = tmp_path / "spec.json"
    prepare.write_json(specification, spec)
    bundle = tmp_path / "bundle"
    prepare.prepare_manifest(
        source,
        bundle,
        image="sha256:" + "0" * 64,
        harness_revisions={name: "1" * 40 for name in ("mini", "ax", "evaluator")},
        spec_path=specification,
    )
    monkeypatch.setattr(
        cache, "ensure_dependency_cache", lambda bundle, **kwargs: cache.reference(bundle)
    )
    return bundle


def test_allowlist_excludes_solutions_history_and_untracked_cache(prepared_bundle, tmp_path):
    bundle = prepared_bundle
    workspace = prepare.create_workspace(bundle, "A1", tmp_path / "workspace")
    files = prepare.file_inventory(workspace)
    assert "Target.lean" in files
    assert ".lake/packages/mathlib/Mathlib/True.lean" in files
    assert ".lake/packages/mathlib/.lake/build/lib/lean/Mathlib/True.olean" in files
    assert ".lake/packages/mathlib/.lake/build/lib/lean/Mathlib/True.ir" in files
    assert ".lake/packages/mathlib/.lake/build/ir/Mathlib/True.c" in files
    assert ".lake/packages/mathlib/.lake/build/ir/Mathlib/True.c.hash" in files
    assert not any("Scratch" in name or "history" in name or "solution" in name for name in files)
    assert "Challenge.lean" not in files
    assert "Prefix.lean" not in files


def test_bundle_mutation_and_additions_are_rejected(prepared_bundle):
    (prepared_bundle / "tasks" / "A1" / "Target.lean").write_text("changed")
    with pytest.raises(ValueError, match="integrity"):
        prepare.load_manifest(prepared_bundle)


def test_bundle_symlink_rejected(prepared_bundle):
    (prepared_bundle / "alias").symlink_to("template")
    with pytest.raises(ValueError, match="Symlinks"):
        prepare.load_manifest(prepared_bundle)


def test_prepared_dependencies_use_offline_paths_and_retain_git_provenance(prepared_bundle):
    lock = json.loads((prepared_bundle / "template" / "lake-manifest.json").read_text())
    assert lock["packages"] == [
        {"type": "path", "name": "mathlib", "dir": ".lake/packages/mathlib"}
    ]
    provenance = prepare.load_manifest(prepared_bundle)["dependency_manifest"]
    assert provenance["packages"] == [{"type": "git", "name": "mathlib", "rev": "a" * 40}]


def test_cached_workspace_uses_verified_native_dependency_links(prepared_bundle):
    reference = cache.reference(prepared_bundle)
    workspace = prepare.create_workspace(
        prepared_bundle,
        "A1",
        prepared_bundle.parent / "cached-workspace",
        dependency_cache=reference,
    )
    assert (workspace / "Target.lean").is_file()
    assert (workspace / ".lake" / "packages" / "mathlib" / "Mathlib").readlink() == Path(
        "/opt/paper-dependencies/mathlib/Mathlib"
    )


def test_cached_workspace_rejects_different_dependency_inventory(prepared_bundle):
    reference = {**cache.reference(prepared_bundle), "dependencies_sha256": "f" * 64}
    destination = prepared_bundle.parent / "wrong-cache-workspace"
    with pytest.raises(ValueError, match="cache does not match"):
        prepare.create_workspace(prepared_bundle, "A1", destination, dependency_cache=reference)
    assert not destination.exists()


def test_verified_native_cache_avoids_rehashing_unused_host_dependencies(
    prepared_bundle, monkeypatch
):
    reference = cache.reference(prepared_bundle)
    original = prepare.sha256

    def hash_runtime_input(path):
        assert not path.is_relative_to(prepared_bundle / "dependencies")
        return original(path)

    monkeypatch.setattr(prepare, "sha256", hash_runtime_input)
    manifest = prepare.load_manifest(prepared_bundle, dependency_cache=reference)
    assert manifest["mathlib_revision"] == "a" * 40


@pytest.mark.parametrize("name", ["template/lakefile.toml", "tasks/A1/Challenge.lean", "extra"])
def test_verified_cache_still_checks_every_non_cache_bundle_file(prepared_bundle, name):
    reference = cache.reference(prepared_bundle)
    (prepared_bundle / name).write_text("modified trusted input")
    with pytest.raises(ValueError, match="integrity"):
        prepare.load_manifest(prepared_bundle, dependency_cache=reference)


@pytest.mark.parametrize("relative", ["../escape", "/absolute"])
def test_relative_path_validation(tmp_path, relative):
    with pytest.raises(ValueError):
        prepare.safe_path(tmp_path, relative)


def test_evaluator_fails_closed_without_docker(prepared_bundle, monkeypatch):
    def unavailable():
        raise RuntimeError("Docker unavailable")

    monkeypatch.setattr(evaluator, "_docker_available", unavailable)
    result = evaluator.evaluate_candidate(
        prepared_bundle, "A1", prepared_bundle / "tasks" / "A1" / "Target.lean"
    )
    assert not result["ok"] and not result["verified"]
    assert result["infrastructure_failure"]


def test_only_candidate_enters_restricted_container(prepared_bundle, monkeypatch):
    seen = []
    monkeypatch.setattr(evaluator, "_docker_available", lambda: None)

    def run(command, **kwargs):
        seen.append(command)
        if command[:2] == ["docker", "run"]:
            mounts = [
                command[index + 1] for index, value in enumerate(command) if value == "--mount"
            ]
            submission = next(value for value in mounts if "dst=/candidate," in value)
            mounted = Path(submission.split(",")[1].removeprefix("src="))
            assert {path.name for path in mounted.iterdir()} == {"Target.lean"}
        return subprocess.CompletedProcess(command, 0, "fake test executor", "")

    monkeypatch.setattr(evaluator.subprocess, "run", run)
    result = evaluator.evaluate_candidate(
        prepared_bundle, "A1", prepared_bundle / "tasks" / "A1" / "Target.lean"
    )
    assert result["verified"]
    docker = seen[0]
    for flag, value in (("--network", "none"), ("--cap-drop", "ALL"), ("--user", "1000:1000")):
        assert docker[docker.index(flag) + 1] == value
    assert "--read-only" in docker
    assert "no-new-privileges" in docker
    assert any("seccomp=" in argument for argument in docker)
    assert any("dst=/bundle/dependencies,readonly" in argument for argument in docker)
    assert result["dependency_cache"] == cache.reference(prepared_bundle)
    assert docker[docker.index("--timeout") + 1] == "600"
    assert result["verification_timeout_seconds"] == 600


def test_timeout_kills_container(prepared_bundle, monkeypatch):
    seen = []
    monkeypatch.setattr(evaluator, "_docker_available", lambda: None)

    def run(command, **kwargs):
        seen.append(command)
        if command[:2] == ["docker", "run"]:
            raise subprocess.TimeoutExpired(command, 1)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(evaluator.subprocess, "run", run)
    result = evaluator.evaluate_candidate(
        prepared_bundle, "A1", prepared_bundle / "tasks" / "A1" / "Target.lean", timeout_seconds=1
    )
    assert not result["verified"] and result["returncode"] == 124
    assert result["infrastructure_failure"]
    assert any(command[:2] == ["docker", "kill"] for command in seen)


def test_staged_dependencies_keep_builds_shared_and_lake_config_writable(tmp_path):
    source = tmp_path / "dependencies"
    library = source / "mathlib"
    (library / ".lake" / "config" / "mathlib").mkdir(parents=True)
    (library / ".lake" / "config" / "mathlib" / "lakefile.olean").write_bytes(b"config")
    (library / ".lake" / "build").mkdir()
    (library / "Mathlib.lean").write_text("import Mathlib.Algebra\n")
    staged = tmp_path / "packages"
    evaluator._stage_dependencies(source, staged)
    assert (staged / "mathlib" / ".lake" / "build").is_symlink()
    assert (staged / "mathlib" / "Mathlib.lean").is_symlink()
    configuration = staged / "mathlib" / ".lake" / "config" / "mathlib" / "lakefile.olean"
    assert not configuration.is_symlink()
    configuration.write_bytes(b"new config")
    assert (library / ".lake" / "config" / "mathlib" / "lakefile.olean").read_bytes() == b"config"


def test_dependency_wrapper_targets_container_cache_without_copying_builds(tmp_path):
    source = tmp_path / "dependencies"
    package = source / "mathlib"
    (package / ".lake" / "build").mkdir(parents=True)
    (package / ".lake" / "lakefile.olean").write_bytes(b"trusted configuration")
    (package / "Mathlib.lean").write_text("import Mathlib.Algebra\n")
    destination = tmp_path / "workspace" / ".lake" / "packages"
    prepare.stage_dependency_links(source, destination, link_root=Path("/opt/paper-dependencies"))
    assert (destination / "mathlib" / "Mathlib.lean").readlink() == Path(
        "/opt/paper-dependencies/mathlib/Mathlib.lean"
    )
    assert (destination / "mathlib" / ".lake" / "build").readlink() == Path(
        "/opt/paper-dependencies/mathlib/.lake/build"
    )
    configuration = destination / "mathlib" / ".lake" / "lakefile.olean"
    assert not configuration.is_symlink()
    configuration.write_bytes(b"updated configuration")
    assert (package / ".lake" / "lakefile.olean").read_bytes() == b"trusted configuration"


def test_preparation_materializes_internal_tracked_dependency_symlinks(tmp_path, monkeypatch):
    source = tmp_path / "source"
    (source / "docs").mkdir(parents=True)
    (source / "README.md").write_text("trusted tracked content")
    link = source / "docs" / "README.md"
    link.symlink_to("../README.md")
    monkeypatch.setattr(
        prepare.subprocess, "check_output", lambda *a, **kw: "README.md\0docs/README.md\0"
    )
    destination = tmp_path / "prepared"
    prepare._copy_dependency(source, destination)
    assert link.is_symlink()
    assert not (destination / "docs" / "README.md").is_symlink()
    assert (destination / "docs" / "README.md").read_text() == "trusted tracked content"


def test_preparation_preserves_proofwidgets_cached_outputs_and_traces(tmp_path, monkeypatch):
    source = tmp_path / "proofwidgets"
    tracked = {"widget/src/widget.tsx", "widget/package-lock.json", "lakefile.lean"}
    generated = {
        ".lake/build/js/widget.js",
        ".lake/build/js/lake.trace",
        ".lake/build/js/shared-library.js",
        "widget/package-lock.json.trace",
        "widget/package-lock.json.hash",
    }
    excluded = {"widget/private.json.trace", ".lake/build/custom/Private.lean"}
    for name in tracked | generated | excluded:
        file = source / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(name)
    monkeypatch.setattr(
        prepare.subprocess, "check_output", lambda *a, **kw: "\0".join(sorted(tracked)) + "\0"
    )
    destination = tmp_path / "prepared"
    prepare._copy_dependency(source, destination)
    assert set(prepare.file_inventory(destination)) == tracked | generated


def test_preparation_requires_warmed_proofwidgets_metadata(prepared_bundle):
    root = prepared_bundle.parent
    spec_path = root / "spec.json"
    spec = json.loads(spec_path.read_text())
    spec["dependency_manifest"] = {
        "packages": [
            {"type": "git", "name": name, "rev": "a" * 40} for name in ("mathlib", "proofwidgets")
        ]
    }
    prepare.write_json(spec_path, spec)
    (root / "source" / ".lake" / "packages" / "proofwidgets").mkdir()
    with pytest.raises(ValueError, match="lake build Mathlib"):
        prepare.prepare_manifest(
            root / "source",
            root / "unwarmed-bundle",
            image="sha256:" + "0" * 64,
            harness_revisions={name: "1" * 40 for name in ("mini", "ax", "evaluator")},
            spec_path=spec_path,
        )


@pytest.mark.parametrize("outside", [False, True])
def test_preparation_rejects_symlinks_to_untracked_or_external_files(
    tmp_path, monkeypatch, outside
):
    source = tmp_path / "source"
    source.mkdir()
    secret = (tmp_path if outside else source) / "secret"
    secret.write_text("untracked contents")
    (source / "link").symlink_to(secret)
    monkeypatch.setattr(prepare.subprocess, "check_output", lambda *a, **kw: "link\0")
    with pytest.raises(ValueError, match="symlink"):
        prepare._copy_dependency(source, tmp_path / "prepared")


def test_seccomp_retains_default_docker_deny_policy_without_sockets():
    profile = json.loads((prepare.ROOT / "configs" / "evaluator-seccomp.json").read_text())
    assert profile["defaultAction"] == "SCMP_ACT_ERRNO"
    allowed = {name for rule in profile["syscalls"] for name in rule["names"]}
    assert not allowed.intersection({"socket", "socketpair", "socketcall"})
    assert "landlock_restrict_self" in allowed


def test_real_audit_cannot_pass_on_missing_infrastructure(prepared_bundle, monkeypatch):
    monkeypatch.setattr(
        evaluator, "_evaluate", lambda *args: {"ok": False, "infrastructure_failure": True}
    )
    audit = evaluator.audit_evaluator(prepared_bundle)
    assert not audit["ok"]
    assert len(audit["cases"]) == 9
    assert audit["kind"] == "real_comparator_audit"
    assert audit["verification_timeout_seconds"] == 600


@pytest.mark.parametrize("failure", [124, 137])
def test_audit_rejects_negative_case_timeout_or_killed_container(
    prepared_bundle, monkeypatch, failure
):
    codes = iter([0, failure, 1, 1, 1, 1, 1, 1, 0])
    monkeypatch.setattr(evaluator, "_docker_available", lambda: None)

    def run(command, **kwargs):
        code = next(codes) if command[:2] == ["docker", "run"] else 0
        return subprocess.CompletedProcess(command, code, "test executor", "")

    monkeypatch.setattr(evaluator.subprocess, "run", run)
    audit = evaluator.audit_evaluator(prepared_bundle)
    assert not audit["ok"]
    assert audit["cases"][1]["infrastructure_failure"]


def test_interrupted_audit_preserves_failed_partial_receipt(prepared_bundle, monkeypatch):
    calls = 0

    def interrupted(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("audit interrupted")
        return {"ok": False, "output": "native failure details"}

    monkeypatch.setattr(evaluator, "_evaluate", interrupted)
    receipt = prepared_bundle.parent / "audit.json"
    with pytest.raises(RuntimeError, match="audit interrupted"):
        evaluator.audit_evaluator(prepared_bundle, output=receipt)
    report = json.loads(receipt.read_text())
    assert not report["ok"]
    assert len(report["cases"]) == 1
    assert report["cases"][0]["output"] == "native failure details"


def test_builtin_manifest_pins_twelve_actual_tasks():
    spec = json.loads(prepare.DEFAULT_SPEC.read_text())
    assert {task["id"] for task in spec["tasks"]} == {
        f"{side}{n}" for side in "AB" for n in range(1, 7)
    }
    assert len(spec["source"]["revision"]) == 40
    assert len(spec["mathlib_revision"]) == 40
    assert all(spec["source_files"][task["source"]] == task["sha256"] for task in spec["tasks"])


def test_builtin_manifest_pins_428rc1_dependencies_and_source_provenance():
    spec = json.loads(prepare.DEFAULT_SPEC.read_text())
    assert spec["lean_toolchain"] == "leanprover/lean4:v4.28.0-rc1"
    assert spec["source_lean_toolchain"] == "leanprover/lean4:v4.26.0"
    assert spec["mathlib_revision"] == "5352afccd6866369be9de43f5b7ec47203555f44"
    packages = {package["name"]: package for package in spec["dependency_manifest"]["packages"]}
    assert packages["mathlib"]["rev"] == spec["mathlib_revision"]
    assert len(packages) == 9
    assert spec["exporter_revision"] == "15f6055e299ad5b89345e533cc2192f4cc00f659"
    assert spec["repl_revision"] == "0bd10273da8a699074308e2b96ff4ec88020b964"


def migrated_spec(prepared_bundle):
    root = prepared_bundle.parent
    path = root / "spec.json"
    spec = json.loads(path.read_text())
    spec["source_lean_toolchain"] = spec["lean_toolchain"]
    spec["lean_toolchain"] = "leanprover/lean4:v4.28.0-rc1"
    spec["dependency_manifest"] = {
        "name": "leanproblems",
        "packages": [{"type": "git", "name": "mathlib", "rev": "a" * 40}],
    }
    prepare.write_json(path, spec)
    return root / "source", path, spec


def test_project_template_migrates_tooling_without_changing_challenges(prepared_bundle):
    source, path, spec = migrated_spec(prepared_bundle)
    before = prepare.file_inventory(source)
    output = prepared_bundle.parent / "migrated-project"
    prepare.write_project_template(source, output, spec_path=path)
    assert prepare.file_inventory(source) == before
    assert set(prepare.file_inventory(output)) == {
        "lean-toolchain",
        "lakefile.toml",
        "lake-manifest.json",
    }
    assert (output / "lean-toolchain").read_text().strip() == spec["lean_toolchain"]
    assert json.loads((output / "lake-manifest.json").read_text()) == spec["dependency_manifest"]


def test_prepared_bundle_uses_migrated_toolchain_and_unchanged_challenge(prepared_bundle):
    source, path, spec = migrated_spec(prepared_bundle)
    (source / ".lake" / "packages" / "mathlib" / "lean-toolchain").write_text(
        spec["lean_toolchain"]
    )
    output = prepared_bundle.parent / "migrated-bundle"
    prepare.prepare_manifest(
        source,
        output,
        image="sha256:" + "0" * 64,
        harness_revisions={name: "1" * 40 for name in ("mini", "ax", "evaluator")},
        spec_path=path,
    )
    assert (output / "tasks" / "A1" / "Challenge.lean").read_bytes() == (
        source / "Task.lean"
    ).read_bytes()
    assert (output / "template" / "lean-toolchain").read_text().strip() == spec["lean_toolchain"]
    manifest = prepare.load_manifest(output)
    assert manifest["source_lean_toolchain"] == "leanprover/lean4:v4.26.0"
    assert manifest["lean_toolchain"] == "leanprover/lean4:v4.28.0-rc1"


def test_migrated_bundle_rejects_old_mathlib_toolchain(prepared_bundle):
    source, path, _ = migrated_spec(prepared_bundle)
    with pytest.raises(ValueError, match="Mathlib toolchain"):
        prepare.prepare_manifest(
            source,
            prepared_bundle.parent / "migrated-bundle",
            image="sha256:" + "0" * 64,
            harness_revisions={name: "1" * 40 for name in ("mini", "ax", "evaluator")},
            spec_path=path,
        )


def test_project_template_rejects_wrong_mathlib_revision(prepared_bundle):
    source, path, spec = migrated_spec(prepared_bundle)
    spec["dependency_manifest"]["packages"][0]["rev"] = "b" * 40
    prepare.write_json(path, spec)
    with pytest.raises(ValueError, match="Mathlib revision"):
        prepare.write_project_template(
            source, prepared_bundle.parent / "migrated-project", spec_path=path
        )
