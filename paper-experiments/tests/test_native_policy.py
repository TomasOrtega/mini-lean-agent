"""Real Lean parser checks, separate from the required sandboxed Comparator audit."""

import json
import os
import subprocess
from pathlib import Path

import pytest

from paper_experiments.prepare import ROOT

LEAN = os.environ.get("PAPER_LEAN")
pytestmark = pytest.mark.skipif(not LEAN, reason="Set PAPER_LEAN to a real Lean 4.28.0-rc1 binary")
PREFIX = "import Lean\ndef preserved : Nat := 42\n"
GOOD = "theorem helper : preserved = 42 := rfl\ntheorem target : preserved = 42 := helper\n"


@pytest.fixture(scope="module", autouse=True)
def matching_lean_version():
    version = subprocess.check_output([LEAN, "--version"], text=True)
    assert "version 4.28.0-rc1," in version


@pytest.mark.parametrize(
    ("candidate", "accepted"),
    [
        (PREFIX + GOOD, True),
        (PREFIX + "theorem target : True := True.intro\n", True),
        (PREFIX.replace("42", "41") + GOOD, False),
        (PREFIX + GOOD + "axiom unrelated : False\n", False),
        (PREFIX + GOOD + "theorem unfinished : True := by sorry\n", False),
        (PREFIX + GOOD + "theorem unused : False := @sorryAx False true\n", True),
        (PREFIX + "@[simp] " + GOOD, False),
        (PREFIX + GOOD + "#eval IO.println 42\n", False),
    ],
)
def test_real_lean_source_policy(tmp_path, candidate, accepted):
    prefix = tmp_path / "Prefix.lean"
    submitted = tmp_path / "Target.lean"
    challenge = tmp_path / "Challenge.lean"
    report = tmp_path / "policy.json"
    prefix.write_text(PREFIX)
    submitted.write_text(candidate)
    challenge.write_text(PREFIX + "theorem target : preserved = 42 := by sorry\n")
    environment = dict(os.environ)
    environment["PATH"] = str(Path(LEAN).parent) + os.pathsep + environment["PATH"]
    result = subprocess.run(
        [
            LEAN,
            "--run",
            str(ROOT / "lean" / "Policy.lean"),
            str(prefix),
            str(submitted),
            str(challenge),
            "target",
            str(report),
        ],
        capture_output=True,
        text=True,
        env=environment,
        timeout=30,
        check=False,
    )
    assert (result.returncode == 0) == accepted, result.stdout + result.stderr
    if accepted:
        data = json.loads(report.read_text())
        assert "target" in data["theorem_names"]
        assert "theorem target : preserved = 42 := by sorry" in data["challenge_source"]
        assert "theorem target : True" not in data["challenge_source"]
