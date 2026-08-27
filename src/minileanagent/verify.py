"""Lean-specific evaluation for the mini-SWE-agent specialization.

The upstream package owns the agent loop, model adapters, bash environment,
limits, and trajectory format. This module only checks the fixed Lean artifact.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import shlex
import subprocess
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

_PLACEHOLDER_PATTERN = re.compile(r"\b(?:sorry|admit|sorryAx)\b")
_WARNING_PATTERN = re.compile(
    r"(?:declaration\s+uses\s+['\"]?sorry|uses\s+['\"]?sorryAx)",
    re.IGNORECASE,
)
_DECLARATION_PATTERN = re.compile(
    r"[A-Za-z_][A-Za-z0-9_']*(?:\.[A-Za-z_][A-Za-z0-9_']*)*\Z"
)


class DeclarationFingerprintError(ValueError):
    """Raised when a protected declaration type cannot be captured."""


@dataclass(frozen=True)
class CheckExecution:
    """Raw result from one Lean checker invocation."""

    command: tuple[str, ...]
    returncode: int
    output: str
    elapsed_seconds: float
    has_sorry_warning: bool


@dataclass(frozen=True)
class VerificationResult:
    """Result of checking one fixed Lean target."""

    ok: bool
    reason: str
    command: tuple[str, ...]
    returncode: int
    output: str
    elapsed_seconds: float
    project: Path
    target: Path
    placeholder: str | None = None
    declaration: str | None = None
    expected_type: str | None = None
    actual_type: str | None = None
    statement_preserved: bool | None = None

    @property
    def expected_type_sha256(self) -> str | None:
        return declaration_type_sha256(self.expected_type)

    @property
    def actual_type_sha256(self) -> str | None:
        return declaration_type_sha256(self.actual_type)

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
            "placeholder": self.placeholder,
            "declaration": self.declaration,
            "expected_type": self.expected_type,
            "expected_type_sha256": self.expected_type_sha256,
            "actual_type": self.actual_type,
            "actual_type_sha256": self.actual_type_sha256,
            "statement_preserved": self.statement_preserved,
        }


def resolve_project(project: Path | str) -> Path:
    """Resolve a Lean project directory."""
    project_path = Path(project).expanduser().resolve()
    if not project_path.is_dir():
        raise ValueError(f"Lean project does not exist or is not a directory: {project_path}")
    return project_path


def resolve_target(project: Path | str, target: Path | str) -> tuple[Path, Path, Path]:
    """Resolve a target and ensure it is a ``.lean`` file inside the project."""
    project_path = resolve_project(project)
    target_input = Path(target).expanduser()
    target_path = (
        target_input.resolve()
        if target_input.is_absolute()
        else (project_path / target_input).resolve()
    )
    try:
        target_relative = target_path.relative_to(project_path)
    except ValueError as exc:
        raise ValueError(f"Target must be inside the project: {target_path}") from exc

    if target_path.suffix != ".lean":
        raise ValueError(f"Target must be a .lean file: {target_relative}")
    if not target_path.is_file():
        raise ValueError(f"Lean target does not exist: {target_relative}")
    return project_path, target_relative, target_path


def validate_declaration_name(declaration: str) -> str:
    """Validate the standard fully-qualified name used by the statement guard."""
    if not _DECLARATION_PATTERN.fullmatch(declaration):
        raise ValueError(
            "Declaration must be a standard fully-qualified Lean name, for example "
            "MyProject.Main.theorem_name"
        )
    return declaration


def build_lean_command(
    project: Path,
    target: Path,
    command_template: str | None = None,
) -> tuple[str, ...]:
    """Build a project-native Lean command without invoking an extra shell."""
    target_text = str(target)
    if command_template:
        tokens = shlex.split(command_template)
        if not tokens:
            raise ValueError("The custom Lean command cannot be empty")
        if any("{target}" in token for token in tokens):
            return tuple(token.replace("{target}", target_text) for token in tokens)
        return (*tokens, target_text)

    if (project / "lakefile.lean").exists() or (project / "lakefile.toml").exists():
        return ("lake", "env", "lean", target_text)
    return ("lean", target_text)


def project_lean_command(
    project: Path,
    target: Path,
    command_template: str | None = None,
) -> tuple[str, ...]:
    """Backward-compatible descriptive alias for :func:`build_lean_command`."""
    return build_lean_command(project, target, command_template)


def strip_lean_comments_and_strings(source: str) -> str:
    """Erase nested comments, line comments, and strings while retaining spacing."""
    out: list[str] = []
    index = 0
    block_depth = 0
    in_line_comment = False
    in_string = False
    escaped = False

    while index < len(source):
        current = source[index]
        following = source[index + 1] if index + 1 < len(source) else ""

        if in_line_comment:
            if current == "\n":
                in_line_comment = False
                out.append("\n")
            else:
                out.append(" ")
            index += 1
            continue

        if block_depth:
            if current == "/" and following == "-":
                block_depth += 1
                out.extend((" ", " "))
                index += 2
            elif current == "-" and following == "/":
                block_depth -= 1
                out.extend((" ", " "))
                index += 2
            else:
                out.append("\n" if current == "\n" else " ")
                index += 1
            continue

        if in_string:
            if escaped:
                escaped = False
            elif current == "\\":
                escaped = True
            elif current == '"':
                in_string = False
            out.append("\n" if current == "\n" else " ")
            index += 1
            continue

        if current == "-" and following == "-":
            in_line_comment = True
            out.extend((" ", " "))
            index += 2
        elif current == "/" and following == "-":
            block_depth = 1
            out.extend((" ", " "))
            index += 2
        elif current == '"':
            in_string = True
            out.append(" ")
            index += 1
        else:
            out.append(current)
            index += 1

    return "".join(out)


def find_placeholder(source: str) -> str | None:
    """Return the first proof-placeholder token outside comments and strings."""
    match = _PLACEHOLDER_PATTERN.search(strip_lean_comments_and_strings(source))
    return match.group(0) if match else None


def normalize_declaration_type(text: str) -> str:
    """Normalize pretty-printer whitespace for a stable before/after comparison."""
    return " ".join(text.split())


def declaration_type_sha256(text: str | None) -> str | None:
    """Hash a normalized declaration type for compact benchmark metadata."""
    if text is None:
        return None
    normalized = normalize_declaration_type(text)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def encode_expected_type(text: str | None) -> str:
    """Encode a declaration type for the submission command's environment."""
    if text is None:
        return ""
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii")


def decode_expected_type(text: str | None) -> str | None:
    """Decode an environment-safe declaration type."""
    if not text:
        return None
    try:
        return base64.b64decode(
            text.encode("ascii"),
            altchars=b"-_",
            validate=True,
        ).decode("utf-8")
    except ValueError as exc:
        raise ValueError("MINI_LEAN_EXPECTED_TYPE is not valid base64-encoded UTF-8") from exc


def _run_local(
    command: tuple[str, ...],
    *,
    project: Path,
    timeout_seconds: int,
    env: Mapping[str, str] | None,
) -> CheckExecution:
    started = time.monotonic()
    process_env = dict(os.environ)
    if env:
        process_env.update(env)

    try:
        completed = subprocess.run(
            command,
            cwd=project,
            env=process_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
        )
        output = completed.stdout or ""
        return CheckExecution(
            command=command,
            returncode=completed.returncode,
            output=output,
            elapsed_seconds=time.monotonic() - started,
            has_sorry_warning=bool(_WARNING_PATTERN.search(output)),
        )
    except FileNotFoundError as exc:
        return CheckExecution(
            command=command,
            returncode=127,
            output=str(exc),
            elapsed_seconds=time.monotonic() - started,
            has_sorry_warning=False,
        )
    except subprocess.TimeoutExpired as exc:
        raw_output = exc.stdout or ""
        output = (
            raw_output.decode(errors="replace")
            if isinstance(raw_output, bytes)
            else raw_output
        )
        return CheckExecution(
            command=command,
            returncode=124,
            output=output,
            elapsed_seconds=time.monotonic() - started,
            has_sorry_warning=False,
        )


def _temporary_augmented_target(target_path: Path, source: str) -> Path:
    descriptor, name = tempfile.mkstemp(
        prefix="MiniLeanFingerprint_",
        suffix=".lean",
        dir=target_path.parent,
        text=True,
    )
    os.close(descriptor)
    temporary = Path(name)
    temporary.write_text(source, encoding="utf-8")
    return temporary


def capture_declaration_type(
    project: Path | str,
    target: Path | str,
    declaration: str,
    *,
    command_template: str | None = None,
    timeout_seconds: int = 120,
    env: Mapping[str, str] | None = None,
) -> str:
    """Capture a declaration's elaborated type without inspecting its proof."""
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    declaration = validate_declaration_name(declaration)
    project_path, _, target_path = resolve_target(project, target)
    source = target_path.read_text(encoding="utf-8")

    nonce = uuid4().hex
    begin = f"MINI_LEAN_TYPE_BEGIN_{nonce}"
    end = f"MINI_LEAN_TYPE_END_{nonce}"
    augmented = (
        source
        + f'\n\n#eval IO.println "{begin}"\n'
        + "set_option pp.universes true in\n"
        + "set_option pp.explicit true in\n"
        + f"#check @{declaration}\n"
        + f'#eval IO.println "{end}"\n'
    )
    temporary = _temporary_augmented_target(target_path, augmented)
    execution: CheckExecution
    try:
        temporary_relative = temporary.relative_to(project_path)
        command = project_lean_command(project_path, temporary_relative, command_template)
        execution = _run_local(
            command,
            project=project_path,
            timeout_seconds=timeout_seconds,
            env=env,
        )
    finally:
        temporary.unlink(missing_ok=True)

    if execution.returncode != 0:
        raise DeclarationFingerprintError(
            "Could not capture declaration type:\n" + execution.output
        )
    start = execution.output.find(begin)
    finish = execution.output.find(end, start + len(begin))
    if start < 0 or finish < 0:
        raise DeclarationFingerprintError(
            "Lean accepted the fingerprint file but did not emit the expected markers:\n"
            + execution.output
        )
    captured = normalize_declaration_type(execution.output[start + len(begin) : finish])
    if not captured:
        raise DeclarationFingerprintError("Lean emitted an empty declaration type")
    return captured


def verify_target(
    project: Path | str,
    target: Path | str,
    *,
    declaration: str | None = None,
    expected_type: str | None = None,
    command_template: str | None = None,
    timeout_seconds: int = 120,
    env: Mapping[str, str] | None = None,
) -> VerificationResult:
    """Check compilation, proof placeholders, and an optional statement guard."""
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    if expected_type is not None and declaration is None:
        raise ValueError("expected_type requires declaration")

    project_path, target_relative, target_path = resolve_target(project, target)
    source = target_path.read_text(encoding="utf-8")
    placeholder = find_placeholder(source)
    command = project_lean_command(project_path, target_relative, command_template)
    execution = _run_local(
        command,
        project=project_path,
        timeout_seconds=timeout_seconds,
        env=env,
    )

    actual_type: str | None = None
    statement_preserved: bool | None = None
    fingerprint_error: str | None = None
    if declaration is not None and execution.returncode == 0:
        try:
            actual_type = capture_declaration_type(
                project_path,
                target_relative,
                declaration,
                command_template=command_template,
                timeout_seconds=timeout_seconds,
                env=env,
            )
            if expected_type is not None:
                statement_preserved = (
                    normalize_declaration_type(actual_type)
                    == normalize_declaration_type(expected_type)
                )
        except DeclarationFingerprintError as exc:
            fingerprint_error = str(exc)
            if expected_type is not None:
                statement_preserved = False

    if execution.returncode == 124:
        ok = False
        reason = f"verification timed out after {timeout_seconds} seconds"
    elif execution.returncode == 127:
        ok = False
        reason = f"verification executable not found: {execution.command[0]}"
    elif execution.returncode != 0:
        ok = False
        reason = "Lean rejected the target"
    elif placeholder is not None:
        ok = False
        reason = f"target contains proof placeholder: {placeholder}"
    elif execution.has_sorry_warning:
        ok = False
        reason = "Lean reported a declaration that uses sorry"
    elif fingerprint_error is not None:
        ok = False
        reason = "could not verify the protected declaration type"
    elif statement_preserved is False:
        ok = False
        reason = "protected declaration type changed"
    else:
        ok = True
        reason = "Lean accepted the target without proof placeholders"
        if statement_preserved is True:
            reason += " and preserved the protected declaration type"

    output = execution.output
    if fingerprint_error:
        output = (output + "\n" + fingerprint_error).strip()

    return VerificationResult(
        ok=ok,
        reason=reason,
        command=execution.command,
        returncode=execution.returncode,
        output=output,
        elapsed_seconds=execution.elapsed_seconds,
        project=project_path,
        target=target_relative,
        placeholder=placeholder,
        declaration=declaration,
        expected_type=normalize_declaration_type(expected_type) if expected_type else None,
        actual_type=actual_type,
        statement_preserved=statement_preserved,
    )
