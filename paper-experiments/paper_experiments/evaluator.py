"""Score only the submitted source in a fresh, restricted Linux container."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from paper_experiments.prepare import (
    ROOT,
    load_manifest,
    sha256,
    stage_dependency_links,
    task_record,
    write_json,
)
from paper_experiments.resources import ensure_container_removed, resolve_resources

MAX_CANDIDATE_BYTES = 2 * 1024 * 1024
VERIFICATION_TIMEOUT_SECONDS = 600


def _stage_dependencies(source: Path, destination: Path) -> None:
    stage_dependency_links(source, destination)


def _docker_available() -> None:
    try:
        result = subprocess.run(
            ["docker", "info", "--format", "{{.OSType}}"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(
            "A running Linux Docker daemon is required for trusted evaluation"
        ) from exc
    if result.returncode or result.stdout.strip() != "linux":
        raise RuntimeError("A running Linux Docker daemon is required for trusted evaluation")


def _evaluate(
    bundle: Path,
    manifest: dict[str, Any],
    candidate: Path,
    challenge_dir: Path,
    declaration: str,
    timeout_seconds: int,
    resources: dict[str, Any] | None = None,
    dependency_cache: dict[str, Any] | None = None,
) -> dict[str, Any]:
    profile = resolve_resources(resources)
    if timeout_seconds <= 0:
        raise ValueError("Evaluation timeout must be positive")
    if candidate.is_symlink() or not candidate.is_file():
        raise ValueError("The candidate must be a regular file, not a symlink")
    source = candidate.read_bytes()
    if len(source) > MAX_CANDIDATE_BYTES:
        return {
            "ok": False,
            "verified": False,
            "reason": "Candidate exceeds the source size limit",
            "returncode": 1,
        }
    started = time.monotonic()
    try:
        _docker_available()
    except RuntimeError as exc:
        return {
            "ok": False,
            "verified": False,
            "reason": str(exc),
            "returncode": 127,
            "infrastructure_failure": True,
        }
    from .cache import cache_mount, ensure_dependency_cache, reference

    if dependency_cache is None:
        dependency_cache = ensure_dependency_cache(bundle, resources=profile.to_dict())
    if dependency_cache != reference(bundle, manifest):
        raise ValueError("Dependency cache does not match the prepared bundle")
    name = "mini-lean-eval-" + uuid.uuid4().hex
    with tempfile.TemporaryDirectory(prefix="mini-lean-candidate-") as temporary:
        submitted = Path(temporary) / "Target.lean"
        submitted.write_bytes(source)
        submitted.chmod(0o444)
        command = [
            "docker",
            "run",
            "--rm",
            "--name",
            name,
            "--network",
            "none",
            "--read-only",
            "--user",
            "1000:1000",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--security-opt",
            f"seccomp={ROOT / 'configs' / 'evaluator-seccomp.json'}",
            "--pids-limit",
            "256",
            *profile.container_args("evaluator"),
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,size=2147483648,mode=1777",
            "--mount",
            f"type=bind,src={bundle.resolve()},dst=/bundle,readonly",
            *cache_mount(dependency_cache, destination="/bundle/dependencies"),
            "--mount",
            f"type=bind,src={challenge_dir.resolve()},dst=/challenge,readonly",
            "--mount",
            f"type=bind,src={submitted.parent},dst=/candidate,readonly",
            "--entrypoint",
            "python3",
            manifest["evaluator_image"],
            "-m",
            "paper_experiments.evaluator",
            "--inside",
            "--declaration",
            declaration,
            "--timeout",
            str(timeout_seconds),
        ]
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=timeout_seconds + 30,
                check=False,
            )
            returncode, output = result.returncode, result.stdout + result.stderr
        except subprocess.TimeoutExpired as exc:
            subprocess.run(["docker", "kill", name], capture_output=True, timeout=15, check=False)
            returncode, output = 124, str(exc)
        finally:
            ensure_container_removed(name, by_name=True)
    return {
        "ok": returncode == 0,
        "verified": returncode == 0,
        "reason": "Trusted evaluator accepted" if returncode == 0 else "Trusted evaluator rejected",
        "returncode": returncode,
        "output": output[-100_000:],
        "elapsed_seconds": time.monotonic() - started,
        "candidate_sha256": hashlib.sha256(source).hexdigest(),
        "bundle_manifest_sha256": sha256(bundle / "manifest.json"),
        "evaluator_image": manifest["evaluator_image"],
        "infrastructure_failure": returncode < 0 or returncode >= 124,
        "resources": profile.to_dict(),
        "resources_sha256": profile.sha256,
        "dependency_cache": dependency_cache,
        "verification_timeout_seconds": timeout_seconds,
    }


def evaluate_candidate(
    bundle: Path,
    task_id: str,
    candidate: Path,
    *,
    timeout_seconds: int = VERIFICATION_TIMEOUT_SECONDS,
    resources: dict[str, Any] | None = None,
    dependency_cache: dict[str, Any] | None = None,
) -> dict[str, Any]:
    manifest = load_manifest(bundle, dependency_cache=dependency_cache)
    task = task_record(manifest, task_id)
    return _evaluate(
        bundle,
        manifest,
        candidate,
        bundle / "tasks" / task_id,
        task["declaration"],
        timeout_seconds,
        resources,
        dependency_cache,
    )


def _inside(declaration: str, timeout_seconds: int) -> int:
    if sys.platform != "linux" or os.geteuid() == 0:
        raise RuntimeError("The evaluator requires unprivileged Linux execution")
    from minileanagent.verify import verify_target

    started = time.monotonic()

    def remaining() -> int:
        seconds = int(timeout_seconds - (time.monotonic() - started))
        if seconds <= 0:
            raise subprocess.TimeoutExpired("trusted evaluation", timeout_seconds)
        return seconds

    work = Path(tempfile.mkdtemp(prefix="trusted-verification-"))
    for filename in ("lakefile.toml", "lake-manifest.json", "lean-toolchain"):
        shutil.copy2(Path("/bundle/template") / filename, work / filename)
    shutil.copy2("/challenge/Challenge.lean", work / "Challenge.lean")
    if sha256(Path("/opt/paper/lean/Policy.lean")) != sha256(Path("/bundle/template/Policy.lean")):
        raise RuntimeError("The image and prepared bundle use different native source policies")
    _stage_dependencies(Path("/bundle/dependencies"), work / ".lake" / "packages")
    policy = subprocess.run(
        [
            "lake",
            "env",
            "lean",
            "--run",
            "/opt/paper/lean/Policy.lean",
            "/challenge/Prefix.lean",
            "/candidate/Target.lean",
            "/challenge/Challenge.lean",
            declaration,
            str(work / "policy-report.json"),
        ],
        cwd=work,
        timeout=remaining(),
        check=False,
    )
    if policy.returncode:
        return 1
    result = verify_target(
        "/candidate",
        "Target.lean",
        verification_project=work,
        declaration=declaration,
        comparator="/opt/comparator/.lake/build/bin/comparator",
        timeout_seconds=remaining(),
    )
    print(json.dumps(result.to_dict(), sort_keys=True))
    if not result.ok:
        return 124 if result.returncode == 124 else 1
    report = json.loads((work / "policy-report.json").read_text())
    if len(report["theorem_names"]) > 1:
        # Helper signatures never enter the immutable target comparison above.
        helper_work = Path(tempfile.mkdtemp(prefix="helper-verification-"))
        for filename in (
            "lakefile.toml",
            "lake-manifest.json",
            "lean-toolchain",
            "policy-report.json",
        ):
            shutil.copy2(work / filename, helper_work / filename)
        (helper_work / "Challenge.lean").write_text(report["challenge_source"])
        _stage_dependencies(Path("/bundle/dependencies"), helper_work / ".lake" / "packages")
        helpers = verify_target(
            "/candidate",
            "Target.lean",
            verification_project=helper_work,
            declaration=declaration,
            comparator="/opt/paper/lean/comparator-wrapper",
            timeout_seconds=remaining(),
        )
        print(json.dumps(helpers.to_dict(), sort_keys=True))
        if not helpers.ok:
            return 124 if helpers.returncode == 124 else 1
    return 0


def audit_evaluator(
    bundle: Path,
    *,
    output: Path | None = None,
    timeout_seconds: int = VERIFICATION_TIMEOUT_SECONDS,
    resources: dict[str, Any] | None = None,
    dependency_cache: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Exercise real Comparator and source-policy decisions; never use fake executables."""
    profile = resolve_resources(resources)
    manifest = load_manifest(bundle, dependency_cache=dependency_cache)
    from .cache import ensure_dependency_cache, reference

    if dependency_cache is None:
        dependency_cache = ensure_dependency_cache(bundle, resources=profile.to_dict())
    if dependency_cache != reference(bundle, manifest):
        raise ValueError("Dependency cache does not match the prepared bundle")
    prefix = "import Mathlib\ndef preserved : Nat := 42\n"
    challenge = prefix + "theorem paper_audit : preserved = 42 := by sorry\n"
    good = prefix + (
        "theorem helper : preserved = 42 := rfl\ntheorem paper_audit : preserved = 42 := helper\n"
    )
    cases = {
        "valid_helper": (good, True),
        "changed_statement": (prefix + "theorem paper_audit : True := True.intro\n", False),
        "changed_definition": (good.replace(": Nat := 42", ": Nat := 41"), False),
        "changed_import": (good.replace("import Mathlib", "import Mathlib\nimport Lean"), False),
        "sorry": (challenge, False),
        "unrelated_axiom": (good + "axiom unused_attack : False\n", False),
        "unused_sorry_helper": (good + "theorem unfinished : True := by sorry\n", False),
        "unused_axiom_helper": (good + "theorem unused : False := @sorryAx False true\n", False),
        "poisoned_build": (good, True),
    }
    results = []
    report = {
        "schema_version": 1,
        "kind": "real_comparator_audit",
        "resources": profile.to_dict(),
        "resources_sha256": profile.sha256,
        "ok": False,
        "evaluator_image": manifest["evaluator_image"],
        "bundle_manifest_sha256": sha256(bundle / "manifest.json"),
        "policy_sha256": sha256(ROOT / "lean" / "Policy.lean"),
        "cases": results,
        "dependency_cache": dependency_cache,
        "verification_timeout_seconds": timeout_seconds,
    }
    if output:
        write_json(output, report)
    with tempfile.TemporaryDirectory(prefix="mini-lean-audit-") as temporary:
        fixture = Path(temporary)
        trusted = fixture / "trusted"
        trusted.mkdir()
        (trusted / "Challenge.lean").write_text(challenge)
        (trusted / "Prefix.lean").write_text(prefix)
        for name, (source, expected) in cases.items():
            agent = fixture / name
            agent.mkdir()
            candidate = agent / "Target.lean"
            candidate.write_text(source)
            if name == "poisoned_build":
                (agent / "lakefile.lean").write_text("this is an invalid malicious Lake file\n")
                (agent / "Challenge.lean").write_text("theorem paper_audit : False := by sorry\n")
                (agent / ".lake").mkdir()
                (agent / ".lake" / "Solution.olean").write_bytes(b"poisoned artifact")
            result = _evaluate(
                bundle,
                manifest,
                candidate,
                trusted,
                "paper_audit",
                timeout_seconds,
                profile.to_dict(),
                dependency_cache,
            )
            results.append({"case": name, "expected": expected, **result})
            if output:
                write_json(output, report)
    report["ok"] = all(
        row["ok"] == row["expected"] and not row.get("infrastructure_failure") for row in results
    )
    if output:
        write_json(output, report)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--inside", action="store_true", required=True)
    parser.add_argument("--declaration", required=True)
    parser.add_argument("--timeout", type=int, default=VERIFICATION_TIMEOUT_SECONDS)
    arguments = parser.parse_args()
    try:
        raise SystemExit(_inside(arguments.declaration, arguments.timeout))
    except subprocess.TimeoutExpired:
        print("Trusted verification timed out", file=sys.stderr)
        raise SystemExit(124) from None
