"""Keep AxProver's compiler and persistent LeanInteract outside the credentialed host."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from typing import Any

from .mini_adapter import DurableDockerEnvironment
from .resources import resolve_resources


class LeanSandbox:
    def __init__(
        self,
        workspace: Path,
        image: str,
        output_dir: Path,
        *,
        resume: dict[str, Any] | None = None,
        resources: dict[str, Any] | None = None,
        dependency_cache: dict[str, Any] | None = None,
    ):
        if "@sha256:" not in image and not image.startswith("sha256:"):
            raise ValueError("The execution image must be pinned by digest")
        if dependency_cache is not None and dependency_cache["image"] != image:
            raise ValueError("Dependency cache and agent image differ")
        profile = resolve_resources(resources)
        from .cache import cache_mount

        self.workspace = workspace.resolve()
        self.output_dir = output_dir
        self.env = DurableDockerEnvironment(
            resume_container=resume["container_id"] if resume else None,
            container_record=output_dir / "agent-container.json",
            image=image,
            cwd=str(self.workspace),
            forward_env=[],
            env={},
            run_args=[
                "--network=none",
                "--cap-drop=ALL",
                "--security-opt=no-new-privileges",
                "--pids-limit=512",
                *profile.container_args("agent"),
                "--mount",
                f"type=bind,src={self.workspace},dst={self.workspace},readonly",
                "--mount",
                f"type=bind,src={self.workspace / '.lake'},dst={self.workspace / '.lake'}",
                "--mount",
                f"type=bind,src={Path(__file__).parent.resolve()},dst=/opt/paper_experiments,readonly",
                *(cache_mount(dependency_cache) if dependency_cache is not None else []),
            ],
            container_timeout="168h",
        )
        self.lock = asyncio.Lock()
        self.invocations = resume["invocations"] if resume else 0
        if not resume:
            subprocess.run(
                [
                    "docker",
                    "exec",
                    "-d",
                    self.env.container_id,
                    "python3",
                    "/opt/paper_experiments/lean_worker.py",
                    "serve",
                    str(self.workspace),
                ],
                check=True,
                capture_output=True,
            )
        else:
            subprocess.run(
                [
                    "docker",
                    "exec",
                    self.env.container_id,
                    "test",
                    "-S",
                    "/tmp/paper-lean-interact.sock",
                ],
                check=True,
                capture_output=True,
            )

    def state(self) -> dict[str, Any]:
        return {"container_id": self.env.container_id, "invocations": self.invocations}

    async def run(self, command: Any) -> Any:
        from lean_interact.interface import CommandResponse, LeanError

        async with self.lock:
            self.invocations += 1
            process = await asyncio.create_subprocess_exec(
                "docker",
                "exec",
                "-i",
                self.env.container_id,
                "timeout",
                "--kill-after=5",
                "180",
                "python3",
                "/opt/paper_experiments/lean_worker.py",
                "request",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(
                        command.model_dump_json(exclude_none=True).encode() + b"\n"
                    ),
                    195,
                )
            except TimeoutError:
                process.kill()
                await process.wait()
                raise
            with (self.output_dir / "lean-interact.jsonl").open("a") as trace:
                trace.write(
                    json.dumps(
                        {
                            "command": command.model_dump(mode="json"),
                            "stdout": stdout.decode(errors="replace"),
                            "stderr": stderr.decode(errors="replace"),
                            "returncode": process.returncode,
                        }
                    )
                    + "\n"
                )
            if process.returncode != 0:
                raise RuntimeError(
                    "Isolated LeanInteract request failed; inspect lean-interact.jsonl"
                )
            data = json.loads(stdout)
            if "error" in data:
                raise RuntimeError(data["error"])
            response = data["response"]
            return (
                LeanError.model_validate(response)
                if "error" in response
                else CommandResponse.model_validate(response)
            )

    async def compile(self, command: list[str], cwd: str, timeout: float) -> tuple[int, str, str]:
        from ax_prover.utils.build import LeanBuildTimeout

        if Path(cwd).resolve() != self.workspace:
            raise ValueError("Compiler working directory escaped the registered workspace")
        self.invocations += 1
        process = await asyncio.create_subprocess_exec(
            "docker",
            "exec",
            "-w",
            cwd,
            self.env.container_id,
            "timeout",
            "--kill-after=5",
            str(max(1, int(timeout))),
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout + 15)
        except TimeoutError as error:
            process.kill()
            await process.wait()
            raise LeanBuildTimeout("Isolated compiler timed out") from error
        if process.returncode in {124, 137}:
            raise LeanBuildTimeout("Isolated compiler timed out")
        return process.returncode, stdout.decode(errors="replace"), stderr.decode(errors="replace")

    async def aclose(self) -> None:
        # The registered continuation reuses this container and its Lean process.
        return None
