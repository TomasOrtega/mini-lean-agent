from __future__ import annotations

from pathlib import Path

import pytest

from minileanagent.verify import (
    build_lean_command,
    capture_declaration_type,
    decode_expected_type,
    encode_expected_type,
    find_placeholder,
    resolve_project,
    resolve_target,
    strip_lean_comments_and_strings,
    verify_target,
)


def test_comment_and_string_stripping_handles_nested_comments() -> None:
    source = """
-- sorry
/- admit /- sorryAx -/ still a comment -/
def message := "sorry and admit"
theorem complete : True := by trivial
"""
    stripped = strip_lean_comments_and_strings(source)
    assert "theorem complete" in stripped
    assert find_placeholder(source) is None


def test_placeholder_detection_finds_only_code_tokens() -> None:
    assert find_placeholder("theorem a : True := by sorry\n") == "sorry"
    assert find_placeholder("theorem b : True := by admit\n") == "admit"
    assert find_placeholder("#check sorryAx\n") == "sorryAx"
    assert find_placeholder("def sorryExample := 1\n") is None


def test_verify_accepts_complete_target(fake_lean_project: tuple[Path, str]) -> None:
    project, command = fake_lean_project
    result = verify_target(project, "Main.lean", command_template=command)
    assert result.ok is True
    assert "accepted" in result.reason
    assert result.returncode == 0
    assert result.target == Path("Main.lean")
    assert result.command[-1] == "Main.lean"


def test_verify_rejects_compiler_failure(fake_lean_project: tuple[Path, str]) -> None:
    project, command = fake_lean_project
    (project / "Main.lean").write_text("BROKEN_CHECKER\n", encoding="utf-8")
    result = verify_target(project, "Main.lean", command_template=command)
    assert result.ok is False
    assert result.reason == "Lean rejected the target"
    assert result.returncode == 1


def test_verify_rejects_source_placeholder(fake_lean_project: tuple[Path, str]) -> None:
    project, command = fake_lean_project
    (project / "Main.lean").write_text(
        "theorem Demo.incomplete : True := by sorry\n",
        encoding="utf-8",
    )
    result = verify_target(project, "Main.lean", command_template=command)
    assert result.ok is False
    assert result.placeholder == "sorry"
    assert "placeholder" in result.reason


def test_verify_rejects_sorry_warning(fake_lean_project: tuple[Path, str]) -> None:
    project, command = fake_lean_project
    (project / "Main.lean").write_text(
        "-- EMIT_SORRY_WARNING\ntheorem Demo.complete : True := by trivial\n",
        encoding="utf-8",
    )
    result = verify_target(project, "Main.lean", command_template=command)
    assert result.ok is False
    assert "reported" in result.reason


def test_verify_timeout(fake_lean_project: tuple[Path, str]) -> None:
    project, command = fake_lean_project
    (project / "Main.lean").write_text("-- SLEEP_CHECKER\n", encoding="utf-8")
    result = verify_target(
        project,
        "Main.lean",
        command_template=command,
        timeout_seconds=1,
    )
    assert result.ok is False
    assert result.returncode == 124
    assert "timed out" in result.reason


def test_missing_executable_is_a_result(fake_lean_project: tuple[Path, str]) -> None:
    project, _ = fake_lean_project
    result = verify_target(
        project,
        "Main.lean",
        command_template="definitely-not-a-real-command",
    )
    assert result.ok is False
    assert result.returncode == 127
    assert "not found" in result.reason


def test_command_resolution(fake_lean_project: tuple[Path, str]) -> None:
    project, _ = fake_lean_project
    target = Path("Main.lean")
    assert build_lean_command(project, target, "custom --flag {target}") == (
        "custom",
        "--flag",
        "Main.lean",
    )
    assert build_lean_command(project, target, "custom --flag") == (
        "custom",
        "--flag",
        "Main.lean",
    )
    assert build_lean_command(project, target) == ("lean", "Main.lean")
    (project / "lakefile.toml").write_text('name = "x"\n', encoding="utf-8")
    assert build_lean_command(project, target) == ("lake", "env", "lean", "Main.lean")
    with pytest.raises(ValueError, match="empty"):
        build_lean_command(project, target, "   ")


def test_project_and_target_boundaries(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="does not exist"):
        resolve_project(tmp_path / "missing")

    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "Outside.lean"
    outside.write_text("#check Nat\n", encoding="utf-8")
    with pytest.raises(ValueError, match="inside"):
        resolve_target(project, outside)
    with pytest.raises(ValueError, match="does not exist"):
        resolve_target(project, "Missing.lean")


def test_statement_fingerprint_is_preserved(fake_lean_project: tuple[Path, str]) -> None:
    project, command = fake_lean_project
    expected = capture_declaration_type(
        project,
        "Main.lean",
        "Demo.complete",
        command_template=command,
    )
    assert expected == "Demo.complete : True"
    result = verify_target(
        project,
        "Main.lean",
        declaration="Demo.complete",
        expected_type=expected,
        command_template=command,
    )
    assert result.ok is True
    assert result.statement_preserved is True
    assert result.expected_type_sha256 == result.actual_type_sha256


def test_statement_fingerprint_rejects_type_change(fake_lean_project: tuple[Path, str]) -> None:
    project, command = fake_lean_project
    result = verify_target(
        project,
        "Main.lean",
        declaration="Demo.complete",
        expected_type="Demo.complete : False",
        command_template=command,
    )
    assert result.ok is False
    assert result.statement_preserved is False
    assert "changed" in result.reason


def test_expected_type_environment_round_trip() -> None:
    original = "Demo.complete : ∀ (A : Type), A → A"
    assert decode_expected_type(encode_expected_type(original)) == original
    assert decode_expected_type("") is None


def test_timeout_must_be_positive(fake_lean_project: tuple[Path, str]) -> None:
    project, command = fake_lean_project
    with pytest.raises(ValueError, match="positive"):
        verify_target(
            project,
            "Main.lean",
            command_template=command,
            timeout_seconds=0,
        )


def test_invalid_declaration_name_is_rejected(fake_lean_project: tuple[Path, str]) -> None:
    project, command = fake_lean_project
    with pytest.raises(ValueError, match="fully-qualified"):
        capture_declaration_type(
            project,
            "Main.lean",
            "not a declaration",
            command_template=command,
        )


def test_expected_type_requires_declaration(fake_lean_project: tuple[Path, str]) -> None:
    project, command = fake_lean_project
    with pytest.raises(ValueError, match="requires declaration"):
        verify_target(
            project,
            "Main.lean",
            expected_type="True",
            command_template=command,
        )


def test_target_must_be_lean_file(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "notes.txt").write_text("hello\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"\.lean"):
        resolve_target(project, "notes.txt")


def test_bad_expected_type_encoding_is_rejected() -> None:
    with pytest.raises(ValueError, match="base64"):
        decode_expected_type("!!!!")


def test_capture_reports_checker_failure(fake_lean_project: tuple[Path, str]) -> None:
    project, command = fake_lean_project
    (project / "Main.lean").write_text("BROKEN_CHECKER\n", encoding="utf-8")
    from minileanagent.verify import DeclarationFingerprintError

    with pytest.raises(DeclarationFingerprintError, match="Could not capture"):
        capture_declaration_type(
            project,
            "Main.lean",
            "Demo.complete",
            command_template=command,
        )


def test_verify_reports_fingerprint_failure(
    fake_lean_project: tuple[Path, str], tmp_path: Path
) -> None:
    project, _ = fake_lean_project
    checker = tmp_path / "no_markers.py"
    checker.write_text("print('accepted without markers')\n", encoding="utf-8")
    import shlex
    import sys

    command = shlex.join([sys.executable, str(checker), "{target}"])
    result = verify_target(
        project,
        "Main.lean",
        declaration="Demo.complete",
        expected_type="Demo.complete : True",
        command_template=command,
    )
    assert result.ok is False
    assert result.statement_preserved is False
    assert "protected declaration" in result.reason
    assert "markers" in result.output
