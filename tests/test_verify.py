from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

from minileanagent import verify


def test_comparator_receives_only_candidate_and_fixed_spec(
    fake_lean_project: tuple[Path, str], fake_comparator: tuple[Path, str]
) -> None:
    project, _ = fake_lean_project
    verifier, comparator = fake_comparator
    challenge = (verifier / "Challenge.lean").read_bytes()
    lakefile = (verifier / "lakefile.toml").read_bytes()
    (project / "lakefile.toml").write_text("untrusted candidate config", encoding="utf-8")
    result = verify.verify_target(
        project,
        "Main.lean",
        verification_project=verifier,
        declaration="Demo.complete",
        comparator=comparator,
    )
    assert result.ok is True
    assert result.returncode == 0
    assert result.command[:3] == ("lake", "env", comparator)
    assert not Path(result.command[-1]).exists()
    assert json.loads(result.output.splitlines()[0]) == {
        "challenge_module": "Challenge",
        "solution_module": "Solution",
        "theorem_names": ["Demo.complete"],
        "permitted_axioms": ["propext", "Quot.sound", "Classical.choice"],
        "enable_nanoda": False,
    }
    assert (verifier / "Solution.lean").read_bytes() == (project / "Main.lean").read_bytes()
    assert (verifier / "Challenge.lean").read_bytes() == challenge
    assert (verifier / "lakefile.toml").read_bytes() == lakefile
    assert result.to_dict()["verification_project"] == str(verifier)
    assert result.to_dict()["declaration"] == "Demo.complete"
    assert result.to_dict()["command"] == list(result.command)


@pytest.mark.parametrize(
    "source", ["BROKEN_CHECKER", "by sorry", "by admit", "sorryAx", "TYPE_FALSE"]
)
def test_comparator_rejection_fails_verification(
    fake_lean_project: tuple[Path, str], fake_comparator: tuple[Path, str], source: str
) -> None:
    project, _ = fake_lean_project
    verifier, comparator = fake_comparator
    (project / "Main.lean").write_text(source, encoding="utf-8")
    result = verify.verify_target(
        project,
        "Main.lean",
        verification_project=verifier,
        declaration="Demo.complete",
        comparator=comparator,
    )
    assert result.ok is False
    assert result.returncode == 1
    assert "rejected" in result.reason
    assert result.output


def test_missing_executable_fails_closed(
    fake_lean_project: tuple[Path, str],
    fake_comparator: tuple[Path, str],
    monkeypatch,
) -> None:
    project, _ = fake_lean_project
    verifier, comparator = fake_comparator

    monkeypatch.setenv("PATH", "")
    result = verify.verify_target(
        project,
        "Main.lean",
        verification_project=verifier,
        declaration="Demo.complete",
        comparator=comparator,
        timeout_seconds=1,
    )
    assert result.ok is False
    assert result.returncode == 127
    assert "not found" in result.reason
    assert result.output
    assert not Path(result.command[-1]).exists()


def test_timeout_stops_comparator_children(
    fake_lean_project: tuple[Path, str], fake_comparator: tuple[Path, str]
) -> None:
    project, _ = fake_lean_project
    verifier, comparator = fake_comparator
    child_code = (
        "from pathlib import Path; import time; "
        "Path('child-started').touch(); time.sleep(1.5); Path('child-survived').touch()"
    )
    Path(comparator).write_text(
        f"#!{sys.executable}\nimport subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}])\n"
        "print('started', flush=True)\ntime.sleep(60)\n",
        encoding="utf-8",
    )
    result = verify.verify_target(
        project,
        "Main.lean",
        verification_project=verifier,
        declaration="Demo.complete",
        comparator=comparator,
        timeout_seconds=1,
    )
    assert result.returncode == 124
    assert result.ok is False
    assert "timed out" in result.reason
    assert "started" in result.output
    assert not Path(result.command[-1]).exists()
    assert (verifier / "child-started").exists()
    time.sleep(1)
    assert not (verifier / "child-survived").exists()


def test_success_is_based_on_exit_code(
    fake_lean_project: tuple[Path, str],
    fake_comparator: tuple[Path, str],
) -> None:
    project, _ = fake_lean_project
    verifier, comparator = fake_comparator
    Path(comparator).write_text(
        f"#!{sys.executable}\nprint('Your solution is okay!')\nraise SystemExit(1)\n",
        encoding="utf-8",
    )
    result = verify.verify_target(
        project,
        "Main.lean",
        verification_project=verifier,
        declaration="Demo.complete",
        comparator=comparator,
    )
    assert result.ok is False


def test_verifier_must_be_separate_prepared_project(
    fake_lean_project: tuple[Path, str],
    fake_comparator: tuple[Path, str],
) -> None:
    project, _ = fake_lean_project
    verifier, _ = fake_comparator
    assert verify.resolve_verification_project(project, verifier) == verifier
    inside = project / "verifier"
    inside.mkdir()
    for invalid in (project, inside):
        with pytest.raises(ValueError, match="outside"):
            verify.resolve_verification_project(project, invalid)
    (verifier / "Challenge.lean").unlink()
    with pytest.raises(ValueError, match=r"Challenge\.lean"):
        verify.resolve_verification_project(project, verifier)
    (verifier / "Challenge.lean").touch()
    (verifier / "lakefile.toml").unlink()
    with pytest.raises(ValueError, match="Lake"):
        verify.resolve_verification_project(project, verifier)


def test_solution_slot_cannot_overwrite_challenge_through_symlink(
    fake_lean_project: tuple[Path, str],
    fake_comparator: tuple[Path, str],
) -> None:
    project, _ = fake_lean_project
    verifier, comparator = fake_comparator
    (verifier / "Solution.lean").symlink_to(verifier / "Challenge.lean")
    with pytest.raises(ValueError, match="symlink"):
        verify.verify_target(
            project,
            "Main.lean",
            verification_project=verifier,
            declaration="Demo.complete",
            comparator=comparator,
        )


def test_replacing_hardlinked_solution_preserves_challenge(
    fake_lean_project: tuple[Path, str], fake_comparator: tuple[Path, str]
) -> None:
    project, _ = fake_lean_project
    verifier, comparator = fake_comparator
    challenge = (verifier / "Challenge.lean").read_bytes()
    (verifier / "Solution.lean").hardlink_to(verifier / "Challenge.lean")
    verify.verify_target(
        project,
        "Main.lean",
        verification_project=verifier,
        declaration="Demo.complete",
        comparator=comparator,
    )
    assert (verifier / "Challenge.lean").read_bytes() == challenge


@pytest.mark.parametrize("declaration", ["", " ", "not a declaration", None])
def test_invalid_declaration_is_rejected(declaration) -> None:
    with pytest.raises(ValueError, match=r"declaration|Declaration"):
        verify.validate_declaration_name(declaration)


def test_timeout_must_be_positive(
    fake_lean_project: tuple[Path, str],
    fake_comparator: tuple[Path, str],
) -> None:
    project, _ = fake_lean_project
    verifier, comparator = fake_comparator
    with pytest.raises(ValueError, match="positive"):
        verify.verify_target(
            project,
            "Main.lean",
            verification_project=verifier,
            declaration="Demo.complete",
            comparator=comparator,
            timeout_seconds=0,
        )


def test_model_check_command_resolution(fake_lean_project: tuple[Path, str]) -> None:
    project, _ = fake_lean_project
    target = Path("Main.lean")
    assert verify.project_lean_command(project, target) == ("lean", "Main.lean")
    (project / "lakefile.toml").touch()
    assert verify.project_lean_command(project, target) == ("lake", "env", "lean", "Main.lean")
    for template in ("custom --flag {target}", "custom --flag"):
        assert verify.project_lean_command(project, target, template) == (
            "custom",
            "--flag",
            "Main.lean",
        )
    with pytest.raises(ValueError, match="empty"):
        verify.project_lean_command(project, target, "   ")


def test_project_and_target_boundaries(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="does not exist"):
        verify.resolve_project(tmp_path / "missing")
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "Outside.lean"
    outside.touch()
    with pytest.raises(ValueError, match="inside"):
        verify.resolve_target(project, outside)
    with pytest.raises(ValueError, match="does not exist"):
        verify.resolve_target(project, "Missing.lean")
    (project / "notes.txt").touch()
    with pytest.raises(ValueError, match=r"\.lean"):
        verify.resolve_target(project, "notes.txt")
