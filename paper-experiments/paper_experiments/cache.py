"""Byte-verified Docker-native copies of the frozen dependency cache."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any

DEPENDENCY_ROOT = "/opt/paper-dependencies"


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe(root: Path, name: str) -> Path:
    relative = Path(name)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise ValueError("Unsafe dependency cache path")
    path = root / relative
    if any(
        (root / Path(*relative.parts[:i])).is_symlink() for i in range(1, len(relative.parts) + 1)
    ):
        raise ValueError("Dependency cache contains a symlink")
    return path


def _files(manifest: dict[str, Any]) -> dict[str, str]:
    result = {
        name.removeprefix("dependencies/"): digest
        for name, digest in manifest["files"].items()
        if name.startswith("dependencies/")
    }
    if not result:
        raise ValueError("Prepared bundle has no dependency cache inventory")
    for name, digest in result.items():
        relative = Path(name)
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or not re.fullmatch("[0-9a-f]{64}", digest)
        ):
            raise ValueError("Invalid dependency cache inventory")
    return result


def reference(bundle: Path, manifest: dict[str, Any] | None = None) -> dict[str, Any]:
    if manifest is None:
        manifest = json.loads((bundle / "manifest.json").read_text())
    identity = {
        "schema": 1,
        "image": manifest["evaluator_image"],
        "dependencies_sha256": _digest(_files(manifest)),
    }
    return {
        **identity,
        "volume": "mini-lean-deps-" + _digest(identity),
        "bundle_manifest_sha256": _sha256(bundle / "manifest.json"),
    }


def cache_mount(receipt: dict[str, Any], *, destination: str = DEPENDENCY_ROOT) -> list[str]:
    identity = {key: receipt[key] for key in ("schema", "image", "dependencies_sha256")}
    if (
        receipt["volume"] != "mini-lean-deps-" + _digest(identity)
        or identity["schema"] != 1
        or not re.fullmatch("[0-9a-f]{64}", identity["dependencies_sha256"])
        or destination not in {DEPENDENCY_ROOT, "/bundle/dependencies"}
    ):
        raise ValueError("Invalid frozen dependency cache reference")
    return [
        "--mount",
        f"type=volume,src={receipt['volume']},dst={destination},readonly,volume-nocopy",
    ]


def verify_tree(root: Path, expected: dict[str, str]) -> None:
    actual = set()
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError("Dependency cache contains a symlink")
        if path.is_file():
            actual.add(path.relative_to(root).as_posix())
    if actual != set(expected):
        raise ValueError("Dependency cache inventory mismatch")
    for name, digest in expected.items():
        if _sha256(_safe(root, name)) != digest:
            raise ValueError(f"Dependency cache hash mismatch: {name}")


def populate_tree(root: Path, expected: dict[str, str], source: Path, image_source: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    if any(root.iterdir()):
        raise ValueError("Refusing to overwrite an existing dependency cache")
    for name, digest in expected.items():
        try:
            image_file = _safe(image_source, name)
        except ValueError:
            image_file = None
        if image_file is not None and image_file.is_file() and _sha256(image_file) == digest:
            original = image_file
        else:
            original = _safe(source, name)
            if not original.is_file() or _sha256(original) != digest:
                raise ValueError(f"Prepared dependency hash mismatch: {name}")
        target = _safe(root, name)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(original, target)


def ensure_container_removed(identifier: str, *, by_name: bool = False) -> None:
    from .resources import ensure_container_removed as remove

    remove(identifier, by_name=by_name)


def ensure_dependency_cache(
    bundle: Path, *, resources: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Provision or verify before active run time, inside the caller's Docker admission."""
    from .resources import resolve_resources

    bundle = bundle.resolve()
    manifest = json.loads((bundle / "manifest.json").read_text())
    expected = _files(manifest)
    receipt = reference(bundle, manifest)
    cache_mount(receipt)
    profile = resolve_resources(resources)
    labels = {
        "org.mini-lean.cache": "1",
        "org.mini-lean.dependencies": receipt["dependencies_sha256"],
        "org.mini-lean.image": receipt["image"],
    }
    lock = Path(tempfile.gettempdir()) / (receipt["volume"] + ".lock")
    with lock.open("a+") as lease:
        fcntl.flock(lease, fcntl.LOCK_EX)
        inspected = subprocess.run(
            ["docker", "volume", "inspect", receipt["volume"]],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        created = inspected.returncode != 0
        if not created:
            details = json.loads(inspected.stdout)[0]
            if any(
                (details.get("Labels") or {}).get(key) != value for key, value in labels.items()
            ):
                raise ValueError("Existing dependency volume has different provenance")
        else:
            subprocess.run(
                [
                    "docker",
                    "volume",
                    "create",
                    *[
                        arg
                        for key, value in labels.items()
                        for arg in ("--label", f"{key}={value}")
                    ],
                    receipt["volume"],
                ],
                capture_output=True,
                text=True,
                check=True,
                timeout=30,
            )
        name = "mini-lean-cache-" + uuid.uuid4().hex
        try:
            with tempfile.TemporaryDirectory(prefix="mini-lean-cache-") as temporary:
                specification = Path(temporary) / "expected.json"
                specification.write_text(json.dumps({"files": expected}))
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
                    "0:0",
                    "--cap-drop",
                    "ALL",
                    "--security-opt",
                    "no-new-privileges",
                    "--pids-limit",
                    "64",
                    "--memory",
                    "512m",
                    "--memory-swap",
                    "512m",
                    "--cpus",
                    str(min(profile.agent_cpus, profile.evaluator_cpus)),
                    "--tmpfs",
                    "/tmp:rw,nosuid,nodev,size=67108864,mode=1777",
                    "--mount",
                    f"type=bind,src={Path(__file__).resolve()},dst=/cache-tool.py,readonly",
                    "--mount",
                    f"type=bind,src={specification},dst=/expected.json,readonly",
                    "--mount",
                    f"type=volume,src={receipt['volume']},dst=/cache,volume-nocopy"
                    + ("" if created else ",readonly"),
                ]
                if created:
                    command.extend(
                        ["--mount", f"type=bind,src={bundle / 'dependencies'},dst=/source,readonly"]
                    )
                command.extend(["--entrypoint", "python3", receipt["image"], "/cache-tool.py"])
                if created:
                    command.append("--populate")
                result = subprocess.run(
                    command, capture_output=True, text=True, check=False, timeout=900
                )
                if result.returncode or json.loads(result.stdout).get("verified") is not True:
                    raise RuntimeError(
                        f"Dependency cache validation failed: {(result.stdout + result.stderr)[-10000:]}"
                    )
        except BaseException:
            ensure_container_removed(name, by_name=True)
            if created:
                subprocess.run(
                    ["docker", "volume", "rm", receipt["volume"]],
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=30,
                )
            raise
        else:
            ensure_container_removed(name, by_name=True)
    return receipt


def _main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--populate", action="store_true")
    args = parser.parse_args()
    expected = json.loads(Path("/expected.json").read_text())["files"]
    if args.populate:
        populate_tree(
            Path("/cache"), expected, Path("/source"), Path("/opt/lean-base/.lake/packages")
        )
    verify_tree(Path("/cache"), expected)
    print(json.dumps({"verified": True, "files": len(expected)}))


if __name__ == "__main__":
    _main()
