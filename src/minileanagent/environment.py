"""Local shell and Lean-checking environment."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import signal
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from minileanagent.types import JsonDict, ToolCall, ToolSchema

SUBMIT_COMMAND = "mini-lean-submit"

BASH_TOOL: ToolSchema = {
    "type": "function",
    "function": {
        "name": "bash",
        "description": (
            "Run one non-interactive bash command at the Lean project root. Each call uses a fresh "
            "shell, while file changes persist. To finish, run `mini-lean-submit` alone, optionally "
            "followed by one project-relative Lean file."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The bash command to execute."}
            },
            "required": ["command"],
            "additionalProperties": False,
        },
    },
}

LEAN_CHECK_TOOL: ToolSchema = {
    "type": "function",
    "function": {
        "name": "lean_check",
        "description": (
            "Check Lean code. Supply exactly one of `path` (a file in the project) or `code` "
            "(a self-contained snippet)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Project-relative path of a Lean file to check.",
                },
                "code": {
                    "type": "string",
                    "description": "A complete Lean snippet, including imports, to check.",
                },
            },
            "additionalProperties": False,
        },
    },
}


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    output: str
    duration_seconds: float
    timed_out: bool = False
    elided_chars: int = 0

    def as_dict(self) -> JsonDict:
        return asdict(self)


@dataclass(frozen=True)
class CheckResult:
    success: bool
    output: str
    returncode: int
    duration_seconds: float
    contains_sorry: bool = False
    backend: str = "local"
    path: str | None = None

    def as_dict(self) -> JsonDict:
        return asdict(self)


class LocalLeanChecker:
    """Check project files using a local Lean installation."""

    def __init__(
        self,
        project: Path,
        *,
        command: str | None = None,
        timeout_seconds: int = 120,
        max_output_chars: int = 20_000,
    ) -> None:
        self.project = project
        self.timeout_seconds = timeout_seconds
        self.max_output_chars = max_output_chars
        self.command = shlex.split(command) if command else self._detect_command()

    def _detect_command(self) -> list[str]:
        has_lakefile = any(
            (self.project / name).exists() for name in ("lakefile.lean", "lakefile.toml")
        )
        if has_lakefile:
            return ["lake", "env", "lean"]
        return ["lean"]

    def check_path(self, path: Path) -> CheckResult:
        relative = path.relative_to(self.project)
        started = time.monotonic()
        process = run_process(
            [*self.command, str(relative)],
            cwd=self.project,
            timeout_seconds=self.timeout_seconds,
            max_output_chars=self.max_output_chars,
        )
        contains_sorry = _contains_sorry_warning(process.output)
        return CheckResult(
            success=process.returncode == 0 and not contains_sorry,
            output=process.output,
            returncode=process.returncode,
            duration_seconds=time.monotonic() - started,
            contains_sorry=contains_sorry,
            backend="local",
            path=str(relative),
        )

    def check_code(self, code: str) -> CheckResult:
        scratch_dir = self.project / ".mini-lean-agent" / "scratch"
        scratch_dir.mkdir(parents=True, exist_ok=True)
        file_descriptor, raw_path = tempfile.mkstemp(
            prefix="Check_", suffix=".lean", dir=scratch_dir, text=True
        )
        path = Path(raw_path)
        try:
            with os.fdopen(file_descriptor, "w", encoding="utf-8") as handle:
                handle.write(code)
            return self.check_path(path)
        finally:
            path.unlink(missing_ok=True)


class KiminaLeanChecker:
    """Check snippets through a Kimina Lean Server.

    Kimina checks the server's configured Lean project. It is therefore ideal for self-contained
    Mathlib snippets, while the local backend remains the reference backend for arbitrary projects.
    """

    def __init__(
        self,
        project: Path,
        *,
        api_url: str | None = None,
        api_key: str | None = None,
        timeout_seconds: int = 120,
    ) -> None:
        try:
            from kimina_client import KiminaClient
        except ImportError as exc:
            raise RuntimeError(
                "Kimina support requires `pip install 'mini-lean-agent[kimina]'`"
            ) from exc
        self.project = project
        self.timeout_seconds = timeout_seconds
        self.client = KiminaClient(
            api_url=api_url,
            api_key=api_key,
            http_timeout=timeout_seconds,
        )

    def check_path(self, path: Path) -> CheckResult:
        return self.check_code(path.read_text(encoding="utf-8"), path=path)

    def check_code(self, code: str, *, path: Path | None = None) -> CheckResult:
        started = time.monotonic()
        response = self.client.check(
            code,
            timeout=self.timeout_seconds,
            show_progress=False,
        )
        payload: JsonDict
        if hasattr(response, "model_dump"):
            payload = response.model_dump(mode="json")
        elif isinstance(response, dict):
            payload = response
        else:  # pragma: no cover - defensive against future SDK changes
            payload = {"response": repr(response)}
        rendered = json.dumps(payload, ensure_ascii=False, indent=2)
        results = payload.get("results") or []
        first = results[0] if results and isinstance(results[0], dict) else {}
        repl_response = first.get("response") or {}
        messages = (
            repl_response.get("messages") or [] if isinstance(repl_response, dict) else []
        )
        has_error_message = any(
            isinstance(message, dict) and message.get("severity") == "error" for message in messages
        )
        contains_sorry = _contains_sorry_warning(rendered)
        success = (
            bool(results)
            and not first.get("error")
            and not has_error_message
            and not contains_sorry
        )
        relative = str(path.relative_to(self.project)) if path is not None else None
        return CheckResult(
            success=success,
            output=rendered,
            returncode=0 if success else 1,
            duration_seconds=time.monotonic() - started,
            contains_sorry=contains_sorry,
            backend="kimina",
            path=relative,
        )


def _contains_sorry_warning(output: str) -> bool:
    lowered = output.lower()
    return "declaration uses 'sorry'" in lowered or 'declaration uses "sorry"' in lowered


def _truncate(output: str, max_chars: int) -> tuple[str, int]:
    if len(output) <= max_chars:
        return output, 0
    half = max_chars // 2
    elided = len(output) - max_chars
    return (
        f"{output[:half]}\n\n... <{elided} characters elided> ...\n\n{output[-half:]}",
        elided,
    )


def run_process(
    args: list[str],
    *,
    cwd: Path,
    timeout_seconds: int,
    max_output_chars: int,
    env: dict[str, str] | None = None,
) -> ProcessResult:
    started = time.monotonic()
    try:
        process = subprocess.Popen(
            args,
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=os.name == "posix",
        )
    except FileNotFoundError as exc:
        return ProcessResult(
            returncode=127,
            output=str(exc),
            duration_seconds=time.monotonic() - started,
        )

    timed_out = False
    try:
        output, _ = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:  # pragma: no cover - primarily used through WSL on Windows
            process.kill()
        output, _ = process.communicate()
        output = f"{output}\nCommand timed out after {timeout_seconds} seconds."

    truncated, elided = _truncate(output, max_output_chars)
    return ProcessResult(
        returncode=124 if timed_out else int(process.returncode or 0),
        output=truncated,
        duration_seconds=time.monotonic() - started,
        timed_out=timed_out,
        elided_chars=elided,
    )


class LocalLeanEnvironment:
    """Stateless bash execution plus an optional structured Lean checker."""

    def __init__(
        self,
        project: Path | str,
        *,
        target: Path | str | None = None,
        toolset: str = "bash",
        checker_backend: str = "local",
        lean_command: str | None = None,
        command_timeout_seconds: int = 120,
        max_output_chars: int = 20_000,
        kimina_url: str | None = None,
        kimina_api_key: str | None = None,
    ) -> None:
        self.project = Path(project).expanduser().resolve()
        if not self.project.is_dir():
            raise ValueError(f"Lean project does not exist: {self.project}")
        self.target = self._resolve_inside(target) if target is not None else None
        if toolset not in {"bash", "lean"}:
            raise ValueError("toolset must be 'bash' or 'lean'")
        self.toolset = toolset
        if command_timeout_seconds <= 0:
            raise ValueError("command_timeout_seconds must be positive")
        if max_output_chars <= 0:
            raise ValueError("max_output_chars must be positive")
        self.command_timeout_seconds = command_timeout_seconds
        self.max_output_chars = max_output_chars
        self.checker_backend = checker_backend

        if checker_backend == "local":
            self.checker: LocalLeanChecker | KiminaLeanChecker = LocalLeanChecker(
                self.project,
                command=lean_command,
                timeout_seconds=command_timeout_seconds,
                max_output_chars=max_output_chars,
            )
        elif checker_backend == "kimina":
            self.checker = KiminaLeanChecker(
                self.project,
                api_url=kimina_url,
                api_key=kimina_api_key,
                timeout_seconds=command_timeout_seconds,
            )
        else:
            raise ValueError("checker_backend must be 'local' or 'kimina'")

    def _resolve_inside(self, path: Path | str) -> Path:
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            candidate = self.project / candidate
        candidate = candidate.resolve()
        try:
            candidate.relative_to(self.project)
        except ValueError as exc:
            raise ValueError(f"Path escapes the project root: {path}") from exc
        return candidate

    def tool_schemas(self) -> list[ToolSchema]:
        tools = [BASH_TOOL]
        if self.toolset == "lean":
            tools.append(LEAN_CHECK_TOOL)
        return tools

    def describe(self) -> JsonDict:
        checker_command: list[str] | None = None
        if isinstance(self.checker, LocalLeanChecker):
            checker_command = self.checker.command
        return {
            "project": str(self.project),
            "target": str(self.target) if self.target else None,
            "toolset": self.toolset,
            "checker_backend": self.checker_backend,
            "checker_command": checker_command,
            "platform": os.name,
        }

    def execute(self, call: ToolCall) -> JsonDict:
        if call.name == "bash":
            return self._execute_bash(call.arguments)
        if call.name == "lean_check" and self.toolset == "lean":
            return self._execute_lean_check(call.arguments)
        return {
            "ok": False,
            "error": f"Unknown or disabled tool: {call.name}",
            "terminal": False,
        }

    def _execute_bash(self, arguments: JsonDict) -> JsonDict:
        command = arguments.get("command")
        if not isinstance(command, str) or not command.strip():
            return {"ok": False, "error": "`command` must be a non-empty string", "terminal": False}
        try:
            is_submit, submit_path = self._parse_submit_command(command)
        except ValueError as exc:
            return {"ok": False, "error": str(exc), "terminal": False}
        if is_submit:
            return self._execute_submit(submit_path)

        env = os.environ.copy()
        env.update(
            {
                "PAGER": "cat",
                "MANPAGER": "cat",
                "GIT_PAGER": "cat",
                "PIP_PROGRESS_BAR": "off",
                "TQDM_DISABLE": "1",
            }
        )
        result = run_process(
            ["bash", "--noprofile", "--norc", "-lc", command],
            cwd=self.project,
            timeout_seconds=self.command_timeout_seconds,
            max_output_chars=self.max_output_chars,
            env=env,
        )
        return {"ok": result.returncode == 0, "terminal": False, **result.as_dict()}

    def _execute_lean_check(self, arguments: JsonDict) -> JsonDict:
        path = arguments.get("path")
        code = arguments.get("code")
        has_path = path is not None
        has_code = code is not None
        if has_path == has_code:
            return {
                "ok": False,
                "error": "Supply exactly one of `path` or `code`",
                "terminal": False,
            }
        if has_path and (not isinstance(path, str) or not path.strip()):
            return {"ok": False, "error": "`path` must be a non-empty string", "terminal": False}
        if has_code and (not isinstance(code, str) or not code.strip()):
            return {"ok": False, "error": "`code` must be a non-empty string", "terminal": False}
        try:
            result = (
                self.checker.check_path(self._resolve_inside(path))
                if isinstance(path, str)
                else self.checker.check_code(code)
            )
        except (OSError, ValueError, RuntimeError) as exc:
            return {"ok": False, "error": str(exc), "terminal": False}
        return {"ok": result.success, "terminal": False, **result.as_dict()}

    @staticmethod
    def _parse_submit_command(command: str) -> tuple[bool, str | None]:
        """Recognize the reserved, stand-alone submission command.

        The first tuple item says whether this is a submission. The second is an optional explicit
        project-relative path; ``None`` selects the configured target.
        """

        try:
            parts = shlex.split(command)
        except ValueError as exc:
            raise ValueError(f"Could not parse bash command: {exc}") from exc
        if not parts or parts[0] != SUBMIT_COMMAND:
            return False, None
        if len(parts) > 2:
            raise ValueError(
                f"Run `{SUBMIT_COMMAND}` alone or with exactly one Lean file. "
                "Do not combine it with another shell command."
            )
        return True, parts[1] if len(parts) == 2 else None

    def _execute_submit(self, raw_path: str | None) -> JsonDict:
        if raw_path is None and self.target is None:
            return {
                "ok": False,
                "error": "No path supplied and no target file was configured",
                "terminal": False,
            }
        try:
            explicit = self._resolve_inside(raw_path) if raw_path is not None else None
            if self.target is not None and explicit is not None and explicit != self.target:
                target_relative = self.target.relative_to(self.project)
                raise ValueError(
                    f"The configured target is {target_relative}; refusing to submit another file"
                )
            path = self.target if self.target is not None else explicit
            assert path is not None
            if not path.is_file():
                raise ValueError(f"Submission file does not exist: {path}")
            result = self.checker.check_path(path)
        except (OSError, ValueError, RuntimeError) as exc:
            return {"ok": False, "error": str(exc), "terminal": False}

        relative = path.relative_to(self.project)
        return {
            "ok": result.success,
            "terminal": result.success,
            "status": "success" if result.success else "rejected",
            "summary": f"Verified {relative}",
            **result.as_dict(),
        }


def doctor(project: Path | str, lean_command: str | None = None) -> JsonDict:
    """Return machine-readable checks used by the CLI doctor command."""

    root = Path(project).expanduser().resolve()
    has_lakefile = any((root / name).exists() for name in ("lakefile.lean", "lakefile.toml"))
    checks: dict[str, Any] = {
        "project_exists": root.is_dir(),
        "lakefile": has_lakefile,
        "lean_toolchain": (root / "lean-toolchain").exists(),
        "bash": shutil.which("bash"),
        "lake": shutil.which("lake"),
        "lean": shutil.which("lean"),
    }
    command = (
        shlex.split(lean_command)
        if lean_command
        else (["lake", "env", "lean"] if has_lakefile else ["lean"])
    )
    executable = shutil.which(command[0]) if command else None
    checks["checker_command"] = command
    checks["checker_executable"] = executable
    checks["ready"] = bool(checks["project_exists"] and checks["bash"] and executable)
    return checks
