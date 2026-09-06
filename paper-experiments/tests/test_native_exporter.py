"""Native format compatibility checks using only trusted, fixed proof fixtures."""

import json
import os
import subprocess
from pathlib import Path

import pytest

VARIABLES = ("PAPER_LEAN", "PAPER_COMPARATOR", "PAPER_EXPORTER", "PAPER_LANDRUN")
pytestmark = pytest.mark.skipif(
    not all(os.environ.get(name) for name in VARIABLES),
    reason="Set PAPER_LEAN, PAPER_COMPARATOR, PAPER_EXPORTER, and PAPER_LANDRUN",
)


@pytest.mark.parametrize(
    ("solution", "accepted"),
    [
        ("theorem target (n : Nat) : n = n := rfl\n", True),
        ("theorem target (n : Nat) : n + 0 = n := Nat.add_zero n\n", False),
        ("theorem target (n : Nat) : True := True.intro\n", False),
        ("axiom bad : False\ntheorem target (n : Nat) : n = n := False.elim bad\n", False),
    ],
)
def test_exporter_and_comparator_agree_on_pinned_proof_toolchain(tmp_path, solution, accepted):
    lean = Path(os.environ["PAPER_LEAN"])
    version = subprocess.check_output([str(lean), "--version"], text=True)
    assert "version 4.28.0-rc1," in version
    (tmp_path / "lean-toolchain").write_text("leanprover/lean4:v4.28.0-rc1\n")
    (tmp_path / "lakefile.toml").write_text(
        'name = "exporter_check"\n[[lean_lib]]\nname = "Challenge"\n'
        '[[lean_lib]]\nname = "Solution"\n'
    )
    (tmp_path / "Challenge.lean").write_text("theorem target (n : Nat) : n = n := by sorry\n")
    (tmp_path / "Solution.lean").write_text(solution)
    (tmp_path / "config.json").write_text(
        json.dumps(
            {
                "challenge_module": "Challenge",
                "solution_module": "Solution",
                "theorem_names": ["target"],
                "permitted_axioms": ["propext", "Quot.sound", "Classical.choice"],
                "enable_nanoda": False,
            }
        )
    )
    environment = dict(os.environ)
    environment["COMPARATOR_LANDRUN"] = os.environ["PAPER_LANDRUN"]
    environment["COMPARATOR_LEAN4EXPORT"] = os.environ["PAPER_EXPORTER"]
    result = subprocess.run(
        [str(lean.with_name("lake")), "env", os.environ["PAPER_COMPARATOR"], "config.json"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    output = result.stdout + result.stderr
    assert "Inductive invalid" not in output, output
    assert result.stdout.count("Exporting") == 2, output
    assert (result.returncode == 0) == accepted, output
