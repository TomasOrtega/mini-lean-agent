from __future__ import annotations

from pathlib import Path

from minileanagent.submit import SUBMISSION_SENTINEL, main


def configure(monkeypatch, project: Path, command: str, target: str = "Main.lean") -> None:
    monkeypatch.setenv("MINI_LEAN_PROJECT", str(project))
    monkeypatch.setenv("MINI_LEAN_TARGET", target)
    monkeypatch.setenv("MINI_LEAN_COMMAND", command)
    monkeypatch.setenv("MINI_LEAN_VERIFY_TIMEOUT", "5")


def test_submit_success(fake_lean_project: tuple[Path, str], monkeypatch, capsys) -> None:
    project, command = fake_lean_project
    configure(monkeypatch, project, command)
    assert main([]) == 0
    output = capsys.readouterr().out.splitlines()
    assert output[0] == SUBMISSION_SENTINEL
    assert output[1].startswith("Verified Main.lean")


def test_submit_rejects_placeholder(
    fake_lean_project: tuple[Path, str], monkeypatch, capsys
) -> None:
    project, command = fake_lean_project
    (project / "Main.lean").write_text(
        "theorem Demo.bad : True := by sorry\n",
        encoding="utf-8",
    )
    configure(monkeypatch, project, command)
    assert main([]) == 1
    captured = capsys.readouterr()
    assert SUBMISSION_SENTINEL not in captured.out
    assert "verification failed" in captured.err.lower()


def test_submit_uses_only_configured_target(
    fake_lean_project: tuple[Path, str], monkeypatch, capsys
) -> None:
    project, command = fake_lean_project
    (project / "Main.lean").write_text(
        "theorem Demo.bad : True := by sorry\n",
        encoding="utf-8",
    )
    (project / "Other.lean").write_text(
        "theorem Demo.easy : True := by trivial\n",
        encoding="utf-8",
    )
    configure(monkeypatch, project, command, target="Main.lean")
    assert main([]) == 1
    assert SUBMISSION_SENTINEL not in capsys.readouterr().out


def test_submit_requires_target(fake_lean_project: tuple[Path, str], monkeypatch, capsys) -> None:
    project, command = fake_lean_project
    monkeypatch.setenv("MINI_LEAN_PROJECT", str(project))
    monkeypatch.setenv("MINI_LEAN_COMMAND", command)
    monkeypatch.delenv("MINI_LEAN_TARGET", raising=False)
    assert main([]) == 2
    assert "MINI_LEAN_TARGET" in capsys.readouterr().err


def test_submit_validates_timeout(fake_lean_project: tuple[Path, str], monkeypatch, capsys) -> None:
    project, command = fake_lean_project
    configure(monkeypatch, project, command)
    monkeypatch.setenv("MINI_LEAN_VERIFY_TIMEOUT", "not-an-int")
    assert main([]) == 2
    assert "integer" in capsys.readouterr().err


def test_submit_rejects_target_argument(capsys) -> None:
    assert main(["Other.lean"]) == 2
    assert "fixed by the harness" in capsys.readouterr().err


def test_submit_help(capsys) -> None:
    assert main(["--help"]) == 0
    assert "usage: mini-lean-submit" in capsys.readouterr().out
