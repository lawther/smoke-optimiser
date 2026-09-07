import json
import os
import subprocess
import sys
from pathlib import Path

from smoke_optimiser.downwind.maps import DownwindMaps
from smoke_optimiser.profiler.models import load_profiling_data_file
from smoke_optimiser.profiler.scope import files_in_scope


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
    # This project configures no testpaths, so pytest collects from the repository
    # root and the profile can only track files pytest itself would collect.
    assert "no configured test paths" in result.stderr
    assert 'testpaths = ["tests"]' in result.stderr

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


def _git_ls_files(project_dir: Path) -> list[str]:
    """Every tracked file, which is what the expiry check enumerates."""
    # git comes from PATH and there is no user input in the arguments
    listed = subprocess.run(["git", "ls-files"], cwd=project_dir, capture_output=True, text=True, check=True)  # noqa: S607
    return listed.stdout.split()


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


def _write_project_with_files_the_maps_cannot_know(project_dir: Path) -> None:
    """A project holding one of everything the scope rule has to separate.

    src/unused.py is measured but never imported, tests/helpers.py is test support
    that no naming pattern would collect, and scripts/tool.py plus data.json are
    tracked files no regeneration would ever teach the maps about.
    """
    project_dir.mkdir()
    (project_dir / "pyproject.toml").write_text(
        """
[project]
name = "my-project"

[tool.pytest.ini_options]
testpaths = ["tests"]
""",
    )
    (project_dir / "data.json").write_text("{}")
    (project_dir / "README.md").write_text("# my project\n")

    scripts_dir = project_dir / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "tool.py").write_text("print('a manual script')\n")

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
    (src_dir / "unused.py").write_text("UNUSED = 1\n")

    tests_dir = project_dir / "tests"
    tests_dir.mkdir()
    (tests_dir / "__init__.py").touch()
    (tests_dir / "helpers.py").write_text("def double(x):\n    return x * 2\n")
    (tests_dir / "test_app.py").write_text(
        """
from src.app import add
from tests.helpers import double

def test_add():
    assert add(1, 2) == 3

def test_double():
    assert double(2) == 4
""",
    )


def _git(project_dir: Path, *args: str) -> None:
    # git comes from PATH and the arguments are this test's own literals
    subprocess.run(["git", *args], cwd=project_dir, check=True, capture_output=True)  # noqa: S603, S607  # noqa: S607


def test_a_profile_regenerated_against_an_unchanged_tree_is_not_expired(tmp_path: Path) -> None:
    """Expiry is divergence, so a tree that has not moved must not read as diverged.

    The check itself is 'is any in-scope file unknown to the maps'. Asserting it
    here, against a real profiling run rather than a hand-built profile, is what
    catches a scope that names files no regeneration could reach -- a profile
    born expired, which would take the full-suite path on every run while looking
    perfectly healthy.
    """
    project_dir = tmp_path / "my_project"
    _write_project_with_files_the_maps_cannot_know(project_dir)
    _git(project_dir, "init")
    _git(project_dir, "add", ".")
    _git(project_dir, "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-m", "initial")

    for attempt in ("profile", "regenerate"):
        result = _run_smoke_optimiser(project_dir, "--profile-only")
        assert result.returncode == 0, f"{attempt} failed: {result.stderr}\nSTDOUT: {result.stdout}"

        with (project_dir / ".smoke_profiling_data.json").open("rb") as f:
            profile = load_profiling_data_file(json.load(f)).to_profiling_data()

        tracked = _git_ls_files(project_dir)
        in_scope = files_in_scope(tracked, profile.scope)
        maps = DownwindMaps.from_profile(profile)

        assert profile.scope.coverage_roots == frozenset({"src"})
        assert profile.scope.test_roots == frozenset({"tests"})
        # A file coverage measured but nothing imported, and test support code no
        # collection pattern matches: both are in scope, so both must be known.
        assert {"src/unused.py", "tests/helpers.py"} <= in_scope
        assert [path for path in sorted(in_scope) if not maps.knows(path)] == [], f"expired after {attempt}"

    # Tracked files no regeneration would teach the maps about must stay out of
    # scope, or the profile is expired the moment it is written.
    assert "scripts/tool.py" not in in_scope
    assert "data.json" not in in_scope
