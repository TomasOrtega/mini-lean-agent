from __future__ import annotations

import sys
from pathlib import Path

import pytest


@pytest.fixture
def lean_project(tmp_path: Path) -> tuple[Path, str]:
    project = tmp_path / "project"
    project.mkdir()
    (project / "lakefile.toml").write_text('name = "Test"\n', encoding="utf-8")
    checker = project / "fake_checker.py"
    checker.write_text(
        """from pathlib import Path
import sys

path = Path(sys.argv[-1])
text = path.read_text(encoding='utf-8')
if 'BROKEN' in text:
    print(f'{path}:1:1: error: synthetic Lean error')
    raise SystemExit(1)
if 'sorry' in text or 'admit' in text:
    print("warning: declaration uses 'sorry'")
print('checked', path)
""",
        encoding="utf-8",
    )
    command = f"{sys.executable} {checker}"
    return project, command
