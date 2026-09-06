"""Build allowlisted, content-pinned benchmark bundles from trusted checkouts."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SPEC = ROOT / "configs" / "putnam2025.json"
_REVISION = re.compile(r"[0-9a-f]{40}\Z")
_IMAGE = re.compile(r"(?:sha256:[0-9a-f]{64}|[^\s]+@sha256:[0-9a-f]{64})\Z")
_EXCLUDED = {".git", ".github", ".gitmodules", ".cache", "__pycache__", ".pytest_cache"}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def safe_path(root: Path, relative: str) -> Path:
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ValueError(f"Expected a relative path: {relative}")
    result = root / path
    if result.is_symlink() or root.resolve() not in result.resolve().parents:
        raise ValueError(f"Path escapes its trusted root: {relative}")
    return result


def git_revision(path: Path) -> str:
    return subprocess.check_output(["git", "-C", str(path), "rev-parse", "HEAD"], text=True).strip()


def file_inventory(root: Path) -> dict[str, str]:
    result = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"Symlinks are forbidden in prepared bundles: {path}")
        if path.is_file():
            result[path.relative_to(root).as_posix()] = sha256(path)
    return result


def _copy_dependency(source: Path, destination: Path) -> None:
    tracked = set(
        subprocess.check_output(["git", "-C", str(source), "ls-files", "-z"], text=True).split("\0")
    )
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        if any(part in _EXCLUDED for part in relative.parts):
            continue
        name = relative.as_posix()
        generated = False
        if name.startswith(".lake/build/lib/lean/"):
            module_file = name.removeprefix(".lake/build/lib/lean/").split(".", 1)[0] + ".lean"
            generated = module_file in tracked and any(
                extension in path.name
                for extension in (".olean", ".ilean", ".ir", ".trace", ".hash")
            )
        elif name.startswith(".lake/build/ir/"):
            module_file = name.removeprefix(".lake/build/ir/").split(".", 1)[0] + ".lean"
            generated = module_file in tracked and path.suffix in {".c", ".o", ".bc", ".hash"}
        elif name.startswith(".lake/build/"):
            generated = path.suffix != ".lean"
        elif path.suffix in {".hash", ".trace"}:
            generated = relative.with_suffix("").as_posix() in tracked
        elif name.startswith(".lake/lakefile.olean") or (
            name.startswith(".lake/config/")
            and path.name
            in {
                "lakefile.olean",
                "lakefile.olean.trace",
            }
        ):
            generated = "lakefile.lean" in tracked
        if name not in tracked and not generated:
            continue
        content = path
        if path.is_symlink():
            content = path.resolve()
            if (
                not content.is_relative_to(source.resolve())
                or content.relative_to(source.resolve()).as_posix() not in tracked
                or not content.is_file()
            ):
                raise ValueError(
                    f"Dependency symlink must resolve to an internal tracked file: {path}"
                )
        if content.is_file():
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(content, target)


def _validated_source(source_project: Path, spec_path: Path | None) -> dict[str, Any]:
    spec = json.loads((spec_path or DEFAULT_SPEC).read_text())
    if git_revision(source_project) != spec["source"]["revision"]:
        raise ValueError("Challenge checkout does not match the pinned source revision")
    for relative, expected in spec["source_files"].items():
        if sha256(safe_path(source_project, relative)) != expected:
            raise ValueError(f"Pinned source changed: {relative}")
    source_toolchain = spec.get("source_lean_toolchain", spec["lean_toolchain"])
    if (source_project / "lean-toolchain").read_text().strip() != source_toolchain:
        raise ValueError("Source Lean toolchain does not match its registered provenance")
    return spec


def _project_files(source_project: Path, spec: dict[str, Any]) -> dict[str, str]:
    manifest = spec.get("dependency_manifest") or json.loads(
        (source_project / "lake-manifest.json").read_text()
    )
    packages = manifest["packages"]
    if len({package["name"] for package in packages}) != len(packages):
        raise ValueError("Dependency names must be unique")
    for package in packages:
        if package.get("type") != "git" or not _REVISION.fullmatch(package["rev"]):
            raise ValueError("Every dependency must have a full Git revision")
    mathlib = [package for package in packages if package["name"] == "mathlib"]
    if len(mathlib) != 1 or mathlib[0]["rev"] != spec["mathlib_revision"]:
        raise ValueError("Dependency manifest Mathlib revision differs from the experiment")
    lakefile = (source_project / "lakefile.toml").read_text()
    if spec.get("dependency_manifest"):
        requirements = tomllib.loads(lakefile).get("require", [])
        for requirement in requirements:
            if requirement["name"] == "mathlib":
                old_revision = requirement["rev"]
                new_revision = mathlib[0].get("inputRev", mathlib[0]["rev"])
                lakefile, replaced = re.subn(
                    rf'(?m)^rev\s*=\s*"{re.escape(old_revision)}"\s*$',
                    f'rev = "{new_revision}"',
                    lakefile,
                )
                if replaced != 1:
                    raise ValueError("Cannot unambiguously migrate the Mathlib requirement")
    lakefile = lakefile.replace('defaultTargets = ["Leanproblems"]', 'defaultTargets = ["Target"]')
    lakefile = lakefile.replace('name = "Leanproblems"', 'name = "Target"')
    lakefile += '\n[[lean_lib]]\nname = "Challenge"\n\n[[lean_lib]]\nname = "Solution"\n'
    return {
        "lean-toolchain": spec["lean_toolchain"] + "\n",
        "lakefile.toml": lakefile,
        "lake-manifest.json": json.dumps(manifest, indent=2) + "\n",
    }


def write_project_template(
    source_project: Path, destination: Path, *, spec_path: Path | None = None
) -> dict[str, Any]:
    """Write the registered proof environment without changing the source checkout."""
    source_project = source_project.resolve()
    spec = _validated_source(source_project, spec_path)
    files = _project_files(source_project, spec)
    destination.mkdir(parents=True, exist_ok=False)
    for name, content in files.items():
        (destination / name).write_text(content)
    return {
        "lean_toolchain": spec["lean_toolchain"],
        "mathlib_revision": spec["mathlib_revision"],
        "source_lean_toolchain": spec.get("source_lean_toolchain", spec["lean_toolchain"]),
        "files": file_inventory(destination),
    }


def prepare_manifest(
    source_project: Path,
    destination: Path,
    *,
    image: str,
    harness_revisions: dict[str, str],
    dependencies: Path | None = None,
    spec_path: Path | None = None,
) -> dict[str, Any]:
    """Copy only pinned challenges/build metadata and authenticated dependencies.

    The source and dependency checkouts must be operator-trusted, never agent workspaces.
    Mathlib's compiled cache is an explicit trust assumption; hashes detect later mutation.
    """
    if not _IMAGE.fullmatch(image):
        raise ValueError("The evaluator image must be pinned by sha256 digest or local image ID")
    if set(harness_revisions) != {"mini", "ax", "evaluator"} or not all(
        _REVISION.fullmatch(value) for value in harness_revisions.values()
    ):
        raise ValueError("Pin full mini, ax, and evaluator Git revisions")
    source_project = source_project.resolve()
    destination = destination.resolve()
    if destination.exists():
        raise FileExistsError(f"Prepared bundle must be new: {destination}")
    spec = _validated_source(source_project, spec_path)
    project_files = _project_files(source_project, spec)
    lake_manifest = json.loads(project_files["lake-manifest.json"])
    packages = lake_manifest["packages"]
    dependency_root = (dependencies or source_project / ".lake" / "packages").resolve()
    for package in packages:
        if package.get("type") != "git" or not _REVISION.fullmatch(package["rev"]):
            raise ValueError("Every dependency must have a full Git revision")
        checkout = safe_path(dependency_root, package["name"])
        if git_revision(checkout) != package["rev"]:
            raise ValueError(f"Dependency revision mismatch: {package['name']}")
        dirty = subprocess.check_output(
            ["git", "-C", str(checkout), "status", "--porcelain", "--untracked-files=no"],
            text=True,
        )
        if dirty.strip():
            raise ValueError(f"Tracked dependency sources are modified: {package['name']}")
        if package["name"] == "proofwidgets" and any(
            not (checkout / name).is_file()
            for name in (
                "widget/package-lock.json.hash",
                "widget/package-lock.json.trace",
                ".lake/build/js/lake.trace",
            )
        ):
            raise ValueError(
                "ProofWidgets cache is incomplete; run lake build Mathlib in the trusted "
                "dependency project before preparation"
            )
    if (dependency_root / "mathlib" / "lean-toolchain").read_text().strip() != spec[
        "lean_toolchain"
    ]:
        raise ValueError("Mathlib toolchain differs from the registered experiment toolchain")
    staging = destination.with_name(destination.name + ".preparing")
    if staging.exists():
        raise FileExistsError(staging)
    staging.mkdir(parents=True)
    try:
        for package in packages:
            _copy_dependency(
                dependency_root / package["name"], staging / "dependencies" / package["name"]
            )
        tasks = []
        for task in spec["tasks"]:
            challenge = safe_path(source_project, task["source"])
            task_dir = staging / "tasks" / task["id"]
            task_dir.mkdir(parents=True)
            source = challenge.read_bytes()
            (task_dir / "Challenge.lean").write_bytes(source)
            (task_dir / "Target.lean").write_bytes(source)
            (task_dir / "Prefix.lean").write_bytes(source[: task["prefix_bytes"]])
            tasks.append(dict(task))
        template = staging / "template"
        template.mkdir()
        for name, content in project_files.items():
            (template / name).write_text(content)
        offline_manifest = {
            **lake_manifest,
            "packages": [
                {
                    **{
                        key: value
                        for key, value in package.items()
                        if key not in {"type", "url", "rev", "inputRev", "subDir"}
                    },
                    "type": "path",
                    "dir": (
                        Path(".lake/packages") / package["name"] / (package.get("subDir") or "")
                    ).as_posix(),
                }
                for package in packages
            ],
        }
        write_json(template / "lake-manifest.json", offline_manifest)
        shutil.copy2(ROOT / "lean" / "Policy.lean", template / "Policy.lean")
        manifest = {
            "schema_version": 1,
            "source": spec["source"],
            "lean_toolchain": spec["lean_toolchain"],
            "source_lean_toolchain": spec.get("source_lean_toolchain", spec["lean_toolchain"]),
            "mathlib_revision": spec["mathlib_revision"],
            "dependency_manifest": lake_manifest,
            "harness_revisions": harness_revisions,
            "evaluator_image": image,
            "comparator_revision": spec["comparator_revision"],
            "exporter_revision": spec["exporter_revision"],
            "exporter_toolchain": spec.get("exporter_toolchain", spec["lean_toolchain"]),
            "landrun_revision": spec["landrun_revision"],
            "lean_interact_version": spec["lean_interact_version"],
            "repl_revision": spec["repl_revision"],
            "tasks": tasks,
            "files": file_inventory(staging),
            "dependency_cache_trust": "operator-trusted build/cache, then byte-pinned",
            "source_policy": "immutable prefix; unmodified theorem commands only; no sorry or metaprogramming commands",
        }
        write_json(staging / "manifest.json", manifest)
        staging.rename(destination)
        return manifest
    except BaseException:
        shutil.rmtree(staging)
        raise


def load_manifest(
    bundle: Path,
    *,
    verify: bool = True,
    dependency_cache: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Verify host inputs; a previously verified native cache supplies dependency bytes."""
    bundle = bundle.resolve()
    manifest = json.loads((bundle / "manifest.json").read_text())
    if dependency_cache is not None:
        from .cache import reference

        if reference(bundle, manifest) != dependency_cache:
            raise ValueError("Dependency cache does not match the prepared bundle")
    if verify:
        if dependency_cache is None:
            actual = file_inventory(bundle)
            expected = manifest["files"]
        else:
            actual = {}
            for entry in bundle.iterdir():
                if entry.is_symlink():
                    raise ValueError(f"Symlinks are forbidden in prepared bundles: {entry}")
                if entry.name == "dependencies":
                    continue
                if entry.is_dir():
                    actual.update(
                        {
                            f"{entry.name}/{name}": digest
                            for name, digest in file_inventory(entry).items()
                        }
                    )
                elif entry.is_file():
                    actual[entry.name] = sha256(entry)
            expected = {
                name: digest
                for name, digest in manifest["files"].items()
                if not name.startswith("dependencies/")
            }
        actual.pop("manifest.json", None)
        if actual != expected:
            raise ValueError("Prepared bundle integrity check failed")
    return manifest


def task_record(manifest: dict[str, Any], task_id: str) -> dict[str, Any]:
    for task in manifest["tasks"]:
        if task["id"] == task_id:
            return task
    raise ValueError(f"Unknown task: {task_id}")


def stage_dependency_links(
    source: Path, destination: Path, *, link_root: Path | None = None
) -> None:
    """Share immutable library files while keeping Lake configuration locks writable."""
    destination.mkdir(parents=True)
    for dependency in source.iterdir():
        project = destination / dependency.name
        origin = (link_root or source) / dependency.name
        project.mkdir()
        for entry in dependency.iterdir():
            if entry.name == ".lake":
                lake = project / ".lake"
                lake.mkdir()
                for artifact in entry.iterdir():
                    target = lake / artifact.name
                    if artifact.name == "config":
                        shutil.copytree(artifact, target)
                    elif artifact.name.startswith("lakefile.olean"):
                        shutil.copy2(artifact, target)
                    else:
                        target.symlink_to(
                            origin / ".lake" / artifact.name, target_is_directory=artifact.is_dir()
                        )
            else:
                (project / entry.name).symlink_to(
                    origin / entry.name, target_is_directory=entry.is_dir()
                )


def create_workspace(
    bundle: Path,
    task_id: str,
    destination: Path,
    *,
    dependency_cache: dict[str, Any] | None = None,
) -> Path:
    manifest = load_manifest(bundle, dependency_cache=dependency_cache)
    task_record(manifest, task_id)
    if dependency_cache is not None:
        from .cache import reference

        if reference(bundle, manifest) != dependency_cache:
            raise ValueError("Dependency cache does not match the prepared bundle")
    if destination.exists():
        raise FileExistsError(destination)
    destination.mkdir(parents=True)
    for name in ("lakefile.toml", "lake-manifest.json", "lean-toolchain"):
        shutil.copy2(bundle / "template" / name, destination / name)
    shutil.copy2(bundle / "tasks" / task_id / "Target.lean", destination / "Target.lean")
    if dependency_cache is None:
        shutil.copytree(bundle / "dependencies", destination / ".lake" / "packages")
    else:
        stage_dependency_links(
            bundle / "dependencies",
            destination / ".lake" / "packages",
            link_root=Path("/opt/paper-dependencies"),
        )
    return destination
