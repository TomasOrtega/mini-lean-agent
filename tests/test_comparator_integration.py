from __future__ import annotations

import os
from pathlib import Path

import pytest

from minileanagent.verify import verify_target

COMPARATOR = os.getenv("MINI_LEAN_TEST_COMPARATOR")
CHALLENGE = "theorem demo (n : Nat) : n = n := by sorry\n"
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not COMPARATOR, reason="Set MINI_LEAN_TEST_COMPARATOR to a real comparator"),
]


@pytest.mark.parametrize(
    ("challenge", "solution", "accepted", "diagnostic"),
    [
        (CHALLENGE, "theorem demo (n : Nat) : n = n := by rfl\n", True, "Your solution is okay!"),
        (CHALLENGE, CHALLENGE, False, "Illegal axiom detected: 'sorryAx'"),
        (
            CHALLENGE,
            "theorem demo : True := by trivial\n",
            False,
            "theorem statement do not match",
        ),
        (
            CHALLENGE,
            "axiom cheating (n : Nat) : n = n\ntheorem demo (n : Nat) : n = n := cheating n\n",
            False,
            "Illegal axiom detected: 'cheating'",
        ),
        (
            "def Goal : Prop := False\ntheorem demo : Goal := by sorry\n",
            "def Goal : Prop := True\ntheorem demo : Goal := by trivial\n",
            False,
            "Const does not match between challenge and target 'Goal'",
        ),
    ],
    ids=["valid-proof", "sorry", "weakened-statement", "new-axiom", "changed-dependency"],
)
def test_real_comparator(
    tmp_path: Path, challenge: str, solution: str, accepted: bool, diagnostic: str
) -> None:
    assert COMPARATOR is not None
    work = tmp_path / "work"
    work.mkdir()
    (work / "Main.lean").write_text(solution)
    verifier = tmp_path / "verifier"
    verifier.mkdir()
    (verifier / "Challenge.lean").write_text(challenge)
    (verifier / "lakefile.toml").write_text(
        'name = "ProofCheck"\n[[lean_lib]]\nname = "Challenge"\n[[lean_lib]]\nname = "Solution"\n'
    )
    (verifier / "lean-toolchain").write_text("leanprover/lean4:v4.33.0\n")

    result = verify_target(
        work,
        "Main.lean",
        declaration="demo",
        verification_project=verifier,
        comparator=COMPARATOR,
    )
    assert result.ok is accepted, result.output
    assert diagnostic in result.output, result.output
    assert (verifier / "Challenge.lean").read_text() == challenge
