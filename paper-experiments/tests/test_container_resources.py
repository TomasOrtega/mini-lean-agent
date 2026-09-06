import json
import subprocess
from types import SimpleNamespace

import pytest
from test_controller import setup
from test_preparation_evaluation import prepared_bundle  # noqa: F401

from paper_experiments import ax_sandbox, evaluator, mini_adapter

PROFILE = {
    "workers": 1,
    "agent_cpus": 2,
    "agent_memory_gib": 6,
    "evaluator_cpus": 2,
    "evaluator_memory_gib": 6,
    "docker_reserve_gib": 2,
}


def assert_limits(command, memory):
    assert command[command.index("--memory") + 1] == memory
    assert command[command.index("--memory-swap") + 1] == memory
    assert command[command.index("--cpus") + 1] == "2"


def test_both_agent_harnesses_receive_the_registered_limits(tmp_path, monkeypatch):
    controller, _, _, candidate, _ = setup(tmp_path)
    calls = []

    def environment(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(container_id="durabletest")

    class Agent:
        def __init__(self, *args, **kwargs):
            assert kwargs["driver_config"]["resources"] == PROFILE
            self.extra_template_vars = {}

        def run(self, task):
            return {}

    monkeypatch.setattr(mini_adapter, "DurableDockerEnvironment", environment)
    monkeypatch.setattr(mini_adapter, "SnapshotAgent", Agent)
    monkeypatch.setattr(ax_sandbox, "DurableDockerEnvironment", environment)
    monkeypatch.setattr(ax_sandbox.subprocess, "run", lambda *a, **k: None)
    mini_adapter.run_mini(
        controller,
        workspace=tmp_path,
        target=candidate,
        declaration="example",
        task="original",
        model_config={"model_name": "gemini/gemini-3.8-flash"},
        image="sha256:" + "a" * 64,
        resources=PROFILE,
    )
    ax_sandbox.LeanSandbox(tmp_path, "sha256:" + "a" * 64, tmp_path, resources=PROFILE)
    assert len(calls) == 2
    for call in calls:
        assert_limits(call["run_args"], "6g")
        assert "--network=none" in call["run_args"]
        assert call["forward_env"] == []


def test_evaluator_uses_separate_registered_memory_cap(prepared_bundle, monkeypatch):  # noqa: F811
    calls = []
    monkeypatch.setattr(evaluator, "_docker_available", lambda: None)

    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "accepted", "")

    monkeypatch.setattr(evaluator.subprocess, "run", run)
    result = evaluator.evaluate_candidate(
        prepared_bundle,
        "A1",
        prepared_bundle / "tasks" / "A1" / "Target.lean",
        resources=PROFILE,
    )
    assert result["verified"]
    assert_limits(calls[0], "6g")
    assert result["resources"] == PROFILE


@pytest.mark.parametrize("changed", ["Memory", "MemorySwap", "NanoCpus"])
def test_resumption_rejects_modified_live_container_limits(monkeypatch, changed):
    environment = object.__new__(mini_adapter.DurableDockerEnvironment)
    environment._resume_container = "frozen123"
    environment.config = SimpleNamespace(
        executable="docker",
        image="sha256:" + "a" * 64,
        run_args=["--cpus", "2", "--memory", "6g", "--memory-swap", "6g"],
    )
    limits = {
        "NetworkMode": "none",
        "Memory": 6 * 1024**3,
        "MemorySwap": 6 * 1024**3,
        "NanoCpus": 2 * 10**9,
    }
    limits[changed] += 1
    details = {
        "State": {"Running": True},
        "HostConfig": limits,
        "Config": {"Image": environment.config.image},
    }
    monkeypatch.setattr(
        mini_adapter.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(stdout=json.dumps([details])),
    )
    with pytest.raises(ValueError, match="resource"):
        environment._start_container()


def test_container_receipt_is_written_before_harness_initialization(tmp_path, monkeypatch):
    environment = object.__new__(mini_adapter.DurableDockerEnvironment)
    environment._resume_container = None
    environment._container_record = tmp_path / "agent-container.json"
    environment.config = SimpleNamespace(run_args=[])
    monkeypatch.setattr(
        mini_adapter.DockerEnvironment,
        "_start_container",
        lambda self: setattr(self, "container_id", "created123"),
    )
    environment._start_container()
    receipt = json.loads(environment._container_record.read_text())
    assert receipt["container_id"] == "created123"
    assert receipt["container_name"].startswith("mini-lean-paper-")


def test_startup_timeout_has_a_durable_name_and_stops_the_queue(tmp_path, monkeypatch):
    from paper_experiments.resources import ContainerCleanupError

    record = tmp_path / "agent-container.json"
    cleaned = []

    def lose_response(self):
        saved = json.loads(record.read_text())
        assert saved["container_name"] == self.config.run_args[-1]
        assert self.config.run_args[-2] == "--name"
        raise subprocess.TimeoutExpired("docker run", 120)

    monkeypatch.setattr(mini_adapter.DockerEnvironment, "_start_container", lose_response)
    monkeypatch.setattr(
        mini_adapter,
        "ensure_container_removed",
        lambda identifier, **kwargs: cleaned.append((identifier, kwargs)),
        raising=False,
    )
    with pytest.raises(ContainerCleanupError, match="startup"):
        mini_adapter.DurableDockerEnvironment(image="sha256:" + "a" * 64, container_record=record)
    assert cleaned == [(json.loads(record.read_text())["container_name"], {"by_name": True})]
