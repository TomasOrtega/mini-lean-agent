"""Registered resource ceilings and process-wide Docker admission checks."""

from __future__ import annotations

import fcntl
import json
import math
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, TextIO

from .protocol import content_hash

GIB = 1024**3
DEFAULT_RESOURCES = Path(__file__).resolve().parents[1] / "configs" / "resources.json"


@dataclass(frozen=True)
class ResourceProfile:
    workers: int
    agent_cpus: int
    agent_memory_gib: int
    evaluator_cpus: int
    evaluator_memory_gib: int
    docker_reserve_gib: int

    def __post_init__(self) -> None:
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
            for value in asdict(self).values()
        ):
            raise ValueError("Resource settings must be positive integers")

    @classmethod
    def load(cls, path: Path = DEFAULT_RESOURCES) -> ResourceProfile:
        return cls.from_dict(json.loads(path.read_text()))

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ResourceProfile:
        return cls(**value)

    def to_dict(self) -> dict[str, int]:
        return asdict(self)

    @property
    def sha256(self) -> str:
        return content_hash(self.to_dict())

    def container_args(self, role: str) -> list[str]:
        if role not in {"agent", "evaluator"}:
            raise ValueError("Unknown container resource role")
        memory = f"{getattr(self, role + '_memory_gib')}g"
        return [
            "--cpus",
            str(getattr(self, role + "_cpus")),
            "--memory",
            memory,
            "--memory-swap",
            memory,
            "--label",
            f"org.mini-lean.paper.role={role}",
            "--label",
            f"org.mini-lean.paper.resources={self.sha256}",
        ]


def resolve_resources(value: dict[str, Any] | None) -> ResourceProfile:
    return ResourceProfile.load() if value is None else ResourceProfile.from_dict(value)


@contextmanager
def execution_lock(path: Path) -> Iterator[TextIO]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError(f"Another experiment command holds {path}") from error
        # Closing the last shared descriptor releases the lease, including after interrupts.
        yield stream


@contextmanager
def docker_execution_lock() -> Iterator[TextIO]:
    with execution_lock(Path(tempfile.gettempdir()) / "mini-lean-paper-docker.lock") as lease:
        yield lease


def docker_capacity() -> dict[str, Any]:
    def output(command: list[str]) -> str:
        try:
            return subprocess.run(
                command, check=True, capture_output=True, text=True, timeout=30
            ).stdout
        except (OSError, subprocess.SubprocessError) as error:
            raise RuntimeError("Docker admission requires a running Linux Docker daemon") from error

    info = json.loads(output(["docker", "info", "--format", "{{json .}} "]))
    if info.get("OSType") != "linux":
        raise RuntimeError("Docker admission requires a Linux daemon")
    identifiers = output(["docker", "ps", "--quiet", "--no-trunc"]).split()
    containers = json.loads(output(["docker", "inspect", *identifiers])) if identifiers else []
    return {"memory_bytes": info["MemTotal"], "cpus": info["NCPU"], "containers": containers}


def preflight(
    profile: ResourceProfile,
    *,
    workers: int | None = None,
    future_retained: int = 0,
    reuse_containers: tuple[str, ...] = (),
    audit_only: bool = False,
    capacity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Reserve all possible concurrent agent/evaluator peaks, including retained RAM."""
    capacity = docker_capacity() if capacity is None else capacity
    count = profile.workers if workers is None else workers
    if count < 0 or count > profile.workers or future_retained < 0:
        raise ValueError("Invalid admission request")
    existing = {}
    for container in capacity["containers"]:
        if not container.get("State", {}).get("Running", True):
            continue
        memory = container["HostConfig"].get("Memory", 0)
        if memory <= 0:
            raise RuntimeError(
                f"Existing container {container.get('Name', container['Id'])} has no memory ceiling; stop it or set a limit before admission"
            )
        existing[container["Id"]] = memory
    agent_bytes = profile.agent_memory_gib * GIB
    reusable = [identifier for identifier in reuse_containers if identifier in existing][:count]
    reuse_credit = sum(min(agent_bytes, existing[identifier]) for identifier in reusable)
    requested_bytes = (
        profile.evaluator_memory_gib * GIB
        if audit_only
        else count * (agent_bytes + profile.evaluator_memory_gib * GIB)
    )
    required = (
        requested_bytes
        + sum(existing.values())
        - reuse_credit
        + (future_retained * profile.agent_memory_gib + profile.docker_reserve_gib) * GIB
    )
    cpu_ceiling = (
        profile.evaluator_cpus
        if audit_only
        else count * (profile.agent_cpus + profile.evaluator_cpus)
    )
    receipt = {
        "resources": profile.to_dict(),
        "resources_sha256": profile.sha256,
        "workers": count,
        "future_retained_containers": future_retained,
        "existing_container_memory_bytes": existing,
        "reused_containers": reusable,
        "required_memory_bytes": required,
        "docker_memory_bytes": capacity["memory_bytes"],
        "requested_cpu_ceiling": cpu_ceiling,
        "docker_cpus": capacity["cpus"],
    }
    if required > capacity["memory_bytes"]:
        recommended_gib = 2 * math.ceil(required / GIB / 2) + 2
        raise RuntimeError(
            f"Docker has {capacity['memory_bytes'] / GIB:.2f} GiB; this queue needs at least {required / GIB:.0f} GiB including existing/retained containers and reserve. "
            f"Use Docker Desktop Resources to allocate at least {recommended_gib} GiB, or choose a smaller profile before audit/registration. "
            "For designated runs, --phase all avoids accumulating frozen containers."
        )
    if cpu_ceiling > capacity["cpus"]:
        raise RuntimeError(
            f"Registered concurrent CPU ceilings require {cpu_ceiling} Docker CPUs; only {capacity['cpus']} are configured"
        )
    return receipt


class ContainerCleanupError(RuntimeError):
    """A live container may still occupy a worker's reserved resources."""


def ensure_container_removed(identifier: str, *, by_name: bool = False) -> dict[str, Any]:
    try:
        removed = subprocess.run(
            ["docker", "rm", "-f", identifier],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if removed.returncode:
            query = f"name={identifier}" if by_name else f"id={identifier}"
            remaining = subprocess.run(
                ["docker", "ps", "--all", "--quiet", "--no-trunc", "--filter", query],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if remaining.returncode or remaining.stdout.strip():
                raise ContainerCleanupError(
                    f"Could not remove container {identifier}; refusing to release its worker slot"
                )
    except (OSError, subprocess.SubprocessError) as error:
        raise ContainerCleanupError(
            f"Could not verify container {identifier} removal; refusing to release its worker slot"
        ) from error
    return {"identifier": identifier, "returncode": 0, "removal_returncode": removed.returncode}
