from dataclasses import replace
from pathlib import Path

import pytest

from paper_experiments.resources import GIB, ResourceProfile, execution_lock, preflight


def capacity(memory_gib=16, containers=()):
    return {"memory_bytes": memory_gib * GIB, "cpus": 14, "containers": list(containers)}


def container(identifier, memory_gib=6):
    return {
        "Id": identifier,
        "Name": identifier,
        "State": {"Running": True},
        "HostConfig": {"Memory": memory_gib * GIB},
    }


def test_default_profile_rejects_current_eight_gib_vm_before_dispatch():
    with pytest.raises(RuntimeError, match=r"needs at least 14 GiB.*at least 16 GiB"):
        preflight(ResourceProfile.load(), capacity=capacity(8))


def test_full_low_only_cannot_accumulate_four_frozen_containers_on_16_gib():
    with pytest.raises(RuntimeError, match="38 GiB"):
        preflight(ResourceProfile.load(), future_retained=4, capacity=capacity())
    receipt = preflight(ResourceProfile.load(), capacity=capacity())
    assert receipt["required_memory_bytes"] == 14 * GIB
    assert receipt["workers"] == 1
    assert receipt["requested_cpu_ceiling"] == 4


def test_explicit_two_worker_profile_remains_supported_with_sufficient_memory():
    profile = replace(ResourceProfile.load(), workers=2)
    with pytest.raises(RuntimeError, match=r"needs at least 26 GiB.*at least 28 GiB"):
        preflight(profile, capacity=capacity(24))
    receipt = preflight(profile, capacity=capacity(28))
    assert receipt["workers"] == 2
    assert receipt["required_memory_bytes"] == 26 * GIB
    assert receipt["requested_cpu_ceiling"] == 8
    with pytest.raises(RuntimeError, match="50 GiB"):
        preflight(profile, future_retained=4, capacity=capacity(28))


def test_existing_and_reused_containers_are_both_budgeted():
    profile = ResourceProfile.load()
    current = capacity(containers=[container("old", 6)])
    with pytest.raises(RuntimeError, match="20 GiB"):
        preflight(profile, capacity=current)
    receipt = preflight(profile, reuse_containers=("old",), capacity=current)
    assert receipt["required_memory_bytes"] == 14 * GIB
    four = capacity(containers=[container(str(index)) for index in range(4)])
    with pytest.raises(RuntimeError, match="32 GiB"):
        preflight(profile, reuse_containers=tuple(map(str, range(4))), capacity=four)


def test_unbounded_unrelated_container_blocks_admission():
    with pytest.raises(RuntimeError, match="no memory ceiling"):
        preflight(ResourceProfile.load(), capacity=capacity(containers=[container("unbounded", 0)]))


def test_container_limits_disable_swapping_and_have_shared_profile_label():
    profile = ResourceProfile.load()
    for role, memory in (("agent", "6g"), ("evaluator", "6g")):
        args = profile.container_args(role)
        assert args[args.index("--memory") + 1] == memory
        assert args[args.index("--memory-swap") + 1] == memory
        assert args[args.index("--cpus") + 1] == "2"
        assert f"org.mini-lean.paper.resources={profile.sha256}" in args


def test_concurrent_invocation_cannot_acquire_execution_lock(tmp_path):
    lock = tmp_path / "experiment.lock"
    with (
        execution_lock(lock),
        pytest.raises(RuntimeError, match="Another experiment"),
        execution_lock(lock),
    ):
        pytest.fail("second invocation entered")
    with execution_lock(lock):
        assert Path(lock).exists()
