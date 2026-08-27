"""Verified completion command used by the bash-only agent."""

from __future__ import annotations

import os
import sys

from minileanagent.verify import VerificationResult, decode_expected_type, verify_target

SUBMISSION_SENTINEL = "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"
SENTINEL = SUBMISSION_SENTINEL


def _required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise ValueError(f"Missing required environment variable: {name}")
    return value


def verify_from_environment() -> VerificationResult:
    """Run the same fixed-target evaluator used after the upstream agent exits."""
    timeout_text = os.getenv("MINI_LEAN_VERIFY_TIMEOUT", "120")
    try:
        timeout = int(timeout_text)
    except ValueError as exc:
        raise ValueError("MINI_LEAN_VERIFY_TIMEOUT must be an integer") from exc

    return verify_target(
        _required_env("MINI_LEAN_PROJECT"),
        _required_env("MINI_LEAN_TARGET"),
        declaration=os.getenv("MINI_LEAN_DECLARATION") or None,
        expected_type=decode_expected_type(os.getenv("MINI_LEAN_EXPECTED_TYPE")),
        command_template=os.getenv("MINI_LEAN_COMMAND") or None,
        timeout_seconds=timeout,
    )


def main(argv: list[str] | None = None) -> int:
    """Emit mini-SWE-agent's sentinel only after the configured target verifies."""
    arguments = sys.argv[1:] if argv is None else argv
    if arguments in (["-h"], ["--help"]):
        print(
            "usage: mini-lean-submit\n\n"
            "Verify the fixed target from the MINI_LEAN_* environment and submit it."
        )
        return 0
    if arguments:
        print(
            "mini-lean-submit accepts no target argument; the target is fixed by the harness",
            file=sys.stderr,
        )
        return 2

    try:
        result = verify_from_environment()
    except (OSError, ValueError) as exc:
        print(f"mini-lean-submit: {exc}", file=sys.stderr)
        return 2

    if not result.ok:
        print(f"Lean verification failed: {result.reason}", file=sys.stderr)
        if result.output:
            end = "" if result.output.endswith("\n") else "\n"
            print(result.output, file=sys.stderr, end=end)
        return 1

    print(SUBMISSION_SENTINEL)
    print(f"Verified {result.target}: {result.reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
