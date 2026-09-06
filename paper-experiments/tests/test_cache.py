import json
from pathlib import Path

import pytest
from test_preparation_evaluation import prepared_bundle  # noqa: F401

from paper_experiments.cache import ensure_dependency_cache


def test_cache_reference_binds_dependency_bytes_and_image(prepared_bundle):  # noqa: F811
    from paper_experiments.cache import reference

    first = reference(prepared_bundle)
    path = prepared_bundle / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["tasks"][0]["id"] = "smoke"
    path.write_text(json.dumps(manifest))
    scoped = reference(prepared_bundle)
    assert scoped["volume"] == first["volume"]
    assert scoped["bundle_manifest_sha256"] != first["bundle_manifest_sha256"]
    name = next(name for name in manifest["files"] if name.startswith("dependencies/"))
    manifest["files"][name] = "b" * 64
    path.write_text(json.dumps(manifest))
    assert reference(prepared_bundle)["volume"] != first["volume"]
    manifest["evaluator_image"] = "sha256:" + "c" * 64
    path.write_text(json.dumps(manifest))
    assert reference(prepared_bundle)["volume"] != scoped["volume"]


def test_cache_verification_rejects_tamper_extra_files_and_symlinks(tmp_path):
    from paper_experiments.cache import verify_tree
    from paper_experiments.prepare import sha256

    root = tmp_path / "cache"
    root.mkdir()
    module = root / "Mathlib.olean"
    module.write_bytes(b"trusted cache")
    expected = {"Mathlib.olean": sha256(module)}
    verify_tree(root, expected)
    module.write_bytes(b"changed")
    with pytest.raises(ValueError, match="hash"):
        verify_tree(root, expected)
    module.write_bytes(b"trusted cache")
    extra = root / "Injected.olean"
    extra.write_bytes(b"extra")
    with pytest.raises(ValueError, match="inventory"):
        verify_tree(root, expected)
    extra.unlink()
    extra.symlink_to(module)
    with pytest.raises(ValueError, match="symlink"):
        verify_tree(root, expected)


def test_seed_prefers_image_only_when_allowlisted_bytes_match(tmp_path):
    from paper_experiments.cache import populate_tree, verify_tree
    from paper_experiments.prepare import sha256

    source, image, target = (tmp_path / name for name in ("source", "image", "target"))
    source.mkdir()
    image.mkdir()
    (source / "Good.olean").write_bytes(b"trusted")
    (image / "Good.olean").write_bytes(b"different build")
    (image / "Extra.olean").write_bytes(b"not registered")
    expected = {"Good.olean": sha256(source / "Good.olean")}
    populate_tree(target, expected, source, image)
    verify_tree(target, expected)
    assert (target / "Good.olean").read_bytes() == b"trusted"
    assert not (target / "Extra.olean").exists()


def test_seed_materialized_bundle_file_overrides_image_symlink(tmp_path):
    from paper_experiments.cache import populate_tree
    from paper_experiments.prepare import sha256

    source, image, target = (tmp_path / name for name in ("source", "image", "target"))
    source.mkdir()
    image.mkdir()
    (source / "tracked.py").write_text("safe tracked source")
    (image / "tracked.py").symlink_to(source / "tracked.py")
    populate_tree(target, {"tracked.py": sha256(source / "tracked.py")}, source, image)
    assert (target / "tracked.py").is_file()
    assert not (target / "tracked.py").is_symlink()


def test_cache_mount_is_readonly_and_refuses_unsafe_reference(prepared_bundle):  # noqa: F811
    from paper_experiments.cache import cache_mount, reference

    receipt = reference(prepared_bundle)
    command = cache_mount(receipt)
    assert command == [
        "--mount",
        f"type=volume,src={receipt['volume']},dst=/opt/paper-dependencies,readonly,volume-nocopy",
    ]
    with pytest.raises(ValueError):
        cache_mount(receipt | {"volume": "other-volume"})
    with pytest.raises(ValueError):
        cache_mount(receipt, destination="/workspace")


def test_cache_provisioning_uses_restricted_container_and_frozen_expected_inventory(
    prepared_bundle,  # noqa: F811
    monkeypatch,
):
    import subprocess

    from paper_experiments import cache

    commands = []

    def run(command, **kwargs):
        commands.append(command)
        if command[:3] == ["docker", "volume", "inspect"]:
            return subprocess.CompletedProcess(command, 1, "", "No such volume")
        if command[:2] == ["docker", "run"]:
            manifest_mount = next(arg for arg in command if "dst=/expected.json" in arg)
            path = Path(manifest_mount.split("src=", 1)[1].split(",", 1)[0])
            assert json.loads(path.read_text())["files"]
            return subprocess.CompletedProcess(command, 0, '{"verified":true}', "")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(cache.subprocess, "run", run)
    monkeypatch.setattr(cache, "ensure_container_removed", lambda *a, **k: None)
    receipt = ensure_dependency_cache(prepared_bundle)
    command = next(command for command in commands if command[:2] == ["docker", "run"])
    assert command[command.index("--network") + 1] == "none"
    assert command[command.index("--memory") + 1] == "512m"
    assert command[command.index("--memory-swap") + 1] == "512m"
    assert "--read-only" in command
    assert receipt == cache.reference(prepared_bundle)


def test_both_harnesses_mount_identical_readonly_dependencies(
    prepared_bundle,  # noqa: F811
    tmp_path,
    monkeypatch,
):
    from types import SimpleNamespace

    from test_controller import setup

    from paper_experiments import ax_sandbox, mini_adapter
    from paper_experiments.cache import cache_mount, reference

    controller, _, _, candidate, _ = setup(tmp_path / "run")
    receipt = reference(prepared_bundle)
    calls = []

    def environment(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(container_id="durabletest")

    class Agent:
        def __init__(self, *args, **kwargs):
            assert kwargs["driver_config"]["dependency_cache"] == receipt
            self.extra_template_vars = {}

        def run(self, task):
            return {}

    monkeypatch.setattr(mini_adapter, "DurableDockerEnvironment", environment)
    monkeypatch.setattr(mini_adapter, "SnapshotAgent", Agent)
    monkeypatch.setattr(ax_sandbox, "DurableDockerEnvironment", environment)
    monkeypatch.setattr(ax_sandbox.subprocess, "run", lambda *args, **kwargs: None)
    mini_adapter.run_mini(
        controller,
        workspace=tmp_path,
        target=candidate,
        declaration="example",
        task="original",
        model_config={"model_name": "gemini/gemini-3.8-flash"},
        image=receipt["image"],
        dependency_cache=receipt,
    )
    ax_sandbox.LeanSandbox(tmp_path, receipt["image"], tmp_path, dependency_cache=receipt)
    for call in calls:
        assert cache_mount(receipt)[1] in call["run_args"]
        assert call["forward_env"] == []
        assert "--network=none" in call["run_args"]


def test_resumed_container_rejects_changed_dependency_volume(monkeypatch, prepared_bundle):  # noqa: F811
    from types import SimpleNamespace

    from paper_experiments import mini_adapter
    from paper_experiments.cache import cache_mount, reference

    receipt = reference(prepared_bundle)
    environment = object.__new__(mini_adapter.DurableDockerEnvironment)
    environment._resume_container = "frozen123"
    environment.config = SimpleNamespace(
        executable="docker",
        image=receipt["image"],
        run_args=["--cpus", "2", "--memory", "6g", "--memory-swap", "6g", *cache_mount(receipt)],
    )
    details = {
        "State": {"Running": True},
        "HostConfig": {
            "NetworkMode": "none",
            "Memory": 6 * 1024**3,
            "MemorySwap": 6 * 1024**3,
            "NanoCpus": 2 * 10**9,
        },
        "Config": {"Image": receipt["image"]},
        "Mounts": [
            {
                "Type": "volume",
                "Name": "wrong",
                "Destination": "/opt/paper-dependencies",
                "RW": False,
            }
        ],
    }
    monkeypatch.setattr(
        mini_adapter.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(stdout=json.dumps([details])),
    )
    with pytest.raises(ValueError, match="cache"):
        environment._start_container()
