import json
import os
import subprocess
import sys
from pathlib import Path


def test_end_to_end_flow(tmp_path: Path) -> None:
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
""",
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
""",
    )

    tests_dir = project_dir / "tests"
    tests_dir.mkdir()
    (tests_dir / "__init__.py").touch()
    (tests_dir / "test_app.py").write_text(
        """
from src.app import add
import os
import pytest

def test_add_positive():
    assert add(1, 2) == 3

def test_add_negative():
    assert add(-1, 2) == 2

@pytest.fixture
def broken():
    add(1, 1)
    raise RuntimeError("boom")

def test_setup_error(broken):
    pass
""",
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

    with smoke_suite_file.open() as f:
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


def _write_project_with_an_uncollectable_test_file(project_dir: Path) -> None:
    """A project whose suite is fine apart from one file that will not import."""
    project_dir.mkdir()
    (project_dir / "pyproject.toml").write_text(
        """
[project]
name = "my-project"
""",
    )

    src_dir = project_dir / "src"
    src_dir.mkdir()
    (src_dir / "__init__.py").touch()
    (src_dir / "app.py").write_text(
        """
def add(a, b):
    if a > 0:
        return a + b
    return b
""",
    )

    tests_dir = project_dir / "tests"
    tests_dir.mkdir()
    (tests_dir / "__init__.py").touch()
    (tests_dir / "test_app.py").write_text(
        """
from src.app import add

def test_add_positive():
    assert add(1, 2) == 3
""",
    )
    (tests_dir / "test_broken.py").write_text(
        """
import a_module_that_does_not_exist

def test_never_collected():
    assert False
""",
    )


def _run_smoke_optimiser(project_dir: Path, *extra_args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path.cwd()) + os.pathsep + str(project_dir)
    return subprocess.run(  # noqa: S603 - the command is a literal plus this test's own arguments
        [sys.executable, "-m", "smoke_optimiser", "--allow-ordered", "--src=src", *extra_args],
        cwd=project_dir,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def test_an_uncollectable_test_file_fails_the_run_instead_of_shrinking_the_suite(tmp_path: Path) -> None:
    """Pytest aborts collection here and runs nothing, which used to look like a result.

    Neither the exit code nor the collection errors were inspected, so the run carried
    on into ingest and reported whatever pytest happened to leave behind -- a smoke
    suite selected from a suite that is quietly smaller than the real one.
    """
    project_dir = tmp_path / "my_project"
    _write_project_with_an_uncollectable_test_file(project_dir)

    result = _run_smoke_optimiser(project_dir)

    assert result.returncode != 0
    assert not (project_dir / ".smoke_suite.json").exists()
    assert "could not collect" in result.stderr
    assert "test_broken.py" in result.stderr


def test_continue_on_collection_errors_still_fails_the_run(tmp_path: Path) -> None:
    """The dangerous case: pytest exits 1, which is also what ordinary failures give.

    With --continue-on-collection-errors pytest runs and measures everything it could
    collect, so the profile looks complete and nothing in the exit code says otherwise.
    Only the collection errors the hook records tell the two apart.
    """
    project_dir = tmp_path / "my_project"
    _write_project_with_an_uncollectable_test_file(project_dir)

    result = _run_smoke_optimiser(project_dir, "--pytest-args=--continue-on-collection-errors")

    assert result.returncode != 0
    assert not (project_dir / ".smoke_suite.json").exists()
    assert "could not collect" in result.stderr
    assert "test_broken.py" in result.stderr


def test_failing_tests_alone_do_not_stop_a_profiling_run(tmp_path: Path) -> None:
    """Exit code 1 from real test failures is expected: failing tests are hard-excluded."""
    project_dir = tmp_path / "my_project"
    _write_project_with_an_uncollectable_test_file(project_dir)
    (project_dir / "tests" / "test_broken.py").write_text(
        """
from src.app import add

def test_that_fails():
    assert add(1, 2) == 4
""",
    )

    result = _run_smoke_optimiser(project_dir)

    assert result.returncode == 0, f"smoke-optimiser failed: {result.stderr}\nSTDOUT: {result.stdout}"
    assert (project_dir / ".smoke_suite.json").exists()
