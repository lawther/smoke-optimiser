import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


def test_end_to_end_flow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test the full flow.

    1. Create a dummy project with code and tests.
    2. Run smoke-optimiser to generate a suite.
    3. Run pytest --smoke to use the suite.
    """
    # 1. Setup dummy project
    project_dir = tmp_path / "my_project"
    project_dir.mkdir()

    # Create pyproject.toml so heuristic can find package name
    (project_dir / "pyproject.toml").write_text(
        """
[project]
name = "my-project"
"""
    )

    src_dir = project_dir / "src"
    src_dir.mkdir()
    (src_dir / "__init__.py").touch()
    (src_dir / "app.py").write_text(
        """
def add(a, b):
    print(f"Adding {a} and {b}")
    if a > 0:
        return a + b
    return b
"""
    )

    tests_dir = project_dir / "tests"
    tests_dir.mkdir()
    (tests_dir / "__init__.py").touch()
    (tests_dir / "test_app.py").write_text(
        """
from src.app import add
import os

def test_add_positive():
    assert add(1, 2) == 3

def test_add_negative():
    assert add(-1, 2) == 2
"""
    )

    # 2. Run smoke-optimiser
    # We need to make sure smoke_optimiser is in sys.path
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path.cwd()) + os.pathsep + str(project_dir)

    # We use --allow-ordered because we don't assume pytest-randomly is installed
    # We NO LONGER pass --cov=src explicitly, testing the heuristic (src/ exists)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "smoke_optimiser",
            "--allow-ordered",
        ],
        cwd=project_dir,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    assert result.returncode == 0, f"smoke-optimiser failed: {result.stderr}\nSTDOUT: {result.stdout}"
    assert "smoke-optimiser results" in result.stdout
    # Verify the warning appeared in stderr (ANSI codes might be stripped by typer in non-tty)
    assert "⚠️ Warning: --src was not specified" in result.stderr
    assert "--src=src" in result.stderr

    smoke_suite_file = project_dir / ".smoke_suite.json"
    assert smoke_suite_file.exists()

    with open(smoke_suite_file) as f:
        data = json.load(f)
        assert len(data["smoke_tests"]) > 0

    # 3. Run pytest --smoke
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--smoke"],
        cwd=project_dir,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    assert result.returncode == 0, f"pytest --smoke failed: {result.stderr}"
    assert "passed" in result.stdout
