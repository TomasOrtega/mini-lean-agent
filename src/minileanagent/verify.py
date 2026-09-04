"""Delegate fixed-target proof verification to Lean FRO's comparator."""

from __future__ import annotations

import json
import os
import re
import shlex
import signal
import subprocess
import tempfile
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

_DECLARATION_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_']*(?:\.[A-Za-z_][A-Za-z0-9_']*)*\Z")


@dataclass(frozen=True)
class VerificationResult:
    ok: bool
    reason: str
    command: tuple[str, ...]
    returncode: int
    output: str
    elapsed_seconds: float
    project: Path
    target: Path
    declaration: str
    verification_project: Path

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "command": list(self.command),
            "returncode": self.returncode,
            "output": self.output,
            "elapsed_seconds": self.elapsed_seconds,
            "project": str(self.project),
            "target": str(self.target),
            "declaration": self.declaration,
            "verification_project": str(self.verification_project),
        }


def resolve_project(project: Path | str) -> Path:
    project_path = Path(project).expanduser().resolve()
    if not project_path.is_dir():
        raise ValueError(f"Lean project does not exist or is not a directory: {project_path}")
    return project_path


def resolve_target(project: Path | str, target: Path | str) -> tuple[Path, Path, Path]:
    project_path = resolve_project(project)
    target_input = Path(target).expanduser()
    target_path = (project_path / target_input).resolve()
    try:
        target_relative = target_path.relative_to(project_path)
    except ValueError as exc:
        raise ValueError(f"Target must be inside the project: {target_path}") from exc
    if target_path.suffix != ".lean":
        raise ValueError(f"Target must be a .lean file: {target_relative}")
    if not target_path.is_file():
        raise ValueError(f"Lean target does not exist: {target_relative}")
    return project_path, target_relative, target_path


def resolve_verification_project(project: Path | str, verification_project: Path | str) -> Path:
    project_path = resolve_project(project)
    verifier = resolve_project(verification_project)
    if verifier == project_path or project_path in verifier.parents:
        raise ValueError("Verification project must be outside the agent's project")
    if not (verifier / "Challenge.lean").is_file():
        raise ValueError("Verification project must contain a trusted Challenge.lean")
    if not any((verifier / name).is_file() for name in ("lakefile.lean", "lakefile.toml")):
        raise ValueError(
            "Verification project must have a Lake configuration for Challenge/Solution"
        )
    return verifier


def validate_declaration_name(declaration: str) -> str:
    if not isinstance(declaration, str) or not _DECLARATION_PATTERN.fullmatch(declaration):
        raise ValueError("Declaration must be a standard fully-qualified Lean name")
    return declaration


def project_lean_command(
    project: Path, target: Path, command_template: str | None = None
) -> tuple[str, ...]:
    """Build the ordinary Lean check command shown to the model."""
    if command_template:
        tokens = shlex.split(command_template)
        if not tokens:
            raise ValueError("The custom Lean command cannot be empty")
        if any("{target}" in token for token in tokens):
            return tuple(token.replace("{target}", str(target)) for token in tokens)
        return (*tokens, str(target))
    if (project / "lakefile.lean").exists() or (project / "lakefile.toml").exists():
        return ("lake", "env", "lean", str(target))
    return ("lean", str(target))


def verify_target(
    project: Path | str,
    target: Path | str,
    *,
    verification_project: Path | str,
    declaration: str,
    comparator: str = "comparator",
    timeout_seconds: int = 120,
) -> VerificationResult:
    """Copy the candidate into a trusted verifier project and run comparator."""
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    declaration = validate_declaration_name(declaration)
    project_path, target_relative, target_path = resolve_target(project, target)
    verifier = resolve_verification_project(project_path, verification_project)
    solution = verifier / "Solution.lean"
    if solution.is_symlink():
        raise ValueError("The Solution.lean candidate slot must not be a symlink")
    solution.unlink(missing_ok=True)
    solution.write_bytes(target_path.read_bytes())
    specification = {
        "challenge_module": "Challenge",
        "solution_module": "Solution",
        "theorem_names": [declaration],
        "permitted_axioms": ["propext", "Quot.sound", "Classical.choice"],
        "enable_nanoda": False,
    }
    started = time.monotonic()
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", prefix="mini-lean-comparator-", dir=verifier, encoding="utf-8"
    ) as config:
        json.dump(specification, config)
        config.flush()
        command = ("lake", "env", comparator, config.name)
        try:
            with subprocess.Popen(
                command,
                cwd=verifier,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                start_new_session=True,
            ) as execution:
                try:
                    output, _ = execution.communicate(timeout=timeout_seconds)
                    returncode = execution.returncode
                    reason = (
                        "Comparator accepted the proof"
                        if returncode == 0
                        else "Comparator rejected the proof"
                    )
                except subprocess.TimeoutExpired:
                    with suppress(ProcessLookupError):
                        os.killpg(execution.pid, signal.SIGKILL)
                    output, _ = execution.communicate()
                    returncode = 124
                    reason = f"Comparator timed out after {timeout_seconds} seconds"
        except FileNotFoundError as exc:
            returncode, output = 127, str(exc)
            reason = "Verification executable not found"

    return VerificationResult(
        ok=returncode == 0,
        reason=reason,
        command=command,
        returncode=returncode,
        output=output,
        elapsed_seconds=time.monotonic() - started,
        project=project_path,
        target=target_relative,
        declaration=declaration,
        verification_project=verifier,
    )
