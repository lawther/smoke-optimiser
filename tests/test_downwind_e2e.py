"""End to end over a real fixture repository: a git edit in, a pytest run out.

Everything here goes through the installed entry point and a real
subprocess, because the parts this issue wires together are exactly the parts
a unit test has to fake: git's view of the tree, the profile written by a
real instrumented run, and pytest's own exit code. A test that mocked any of
those would be asserting the wiring against itself.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from smoke_optimiser.downwind.blind_spots import BlindSpotReason
from smoke_optimiser.reports.downwind_suite import DownwindSuiteFile, read_downwind_suite

EXIT_OK = 0
EXIT_TESTS_FAILED = 1
EXIT_ERROR = 1


def _git(project_dir: Path, *args: str) -> None:
    # git comes from PATH and the arguments are this test's own literals
    subprocess.run(["git", *args], cwd=project_dir, check=True, capture_output=True)  # noqa: S603, S607


def _env(project_dir: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path.cwd()) + os.pathsep + str(project_dir)
    return env


def _run(project_dir: Path, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - a literal command plus this test's own arguments
        [sys.executable, "-m", "smoke_optimiser", *args],
        cwd=cwd or project_dir,
        capture_output=True,
        text=True,
        env=_env(project_dir),
        check=False,
    )


def _write_project(project_dir: Path) -> None:
    """Two source modules with a test module each, so a selection is visibly a subset.

    Kept deliberately separable: nothing in app.py reaches test_other.py by
    any relation, so 'only the tests downwind ran' is a claim the fixture can
    actually make.
    """
    project_dir.mkdir(parents=True)
    (project_dir / "pyproject.toml").write_text(
        '[project]\nname = "my-project"\n\n[tool.pytest.ini_options]\ntestpaths = ["tests"]\npythonpath = ["."]\n',
    )

    src = project_dir / "src"
    src.mkdir()
    (src / "__init__.py").touch()
    (src / "app.py").write_text("def add(a, b):\n    if a > 0:\n        return a + b\n    return b\n")
    (src / "other.py").write_text("def double(x):\n    if x > 0:\n        return x * 2\n    return 0\n")

    tests = project_dir / "tests"
    tests.mkdir()
    (tests / "__init__.py").touch()
    (tests / "test_app.py").write_text(
        "from src.app import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n\n\n"
        "def test_add_negative():\n    assert add(-1, 2) == 2\n",
    )
    (tests / "test_other.py").write_text(
        "from src.other import double\n\n\ndef test_double():\n    assert double(2) == 4\n",
    )


@pytest.fixture
def profiled_repo(tmp_path: Path) -> Path:
    """A committed fixture repository with a fresh profile beside it.

    Committed first and profiled second, so the tree is clean at the moment
    the profile is taken: every test below starts from 'nothing has changed'
    and makes exactly the one change it is about.
    """
    project_dir = tmp_path / "my_project"
    _write_project(project_dir)
    _git(project_dir, "init")
    _git(project_dir, "add", ".")
    _git(project_dir, "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-m", "initial")

    result = _run(project_dir, "smoke", "--profile-only", "--allow-ordered", "--src=src")
    assert result.returncode == EXIT_OK, f"profiling failed: {result.stderr}\nSTDOUT: {result.stdout}"
    # Our own untracked artefacts must not read as changes, and neither must
    # Python's: git reports every .pyc under --untracked-files=all, and each one
    # is a non-Python file the maps cannot answer for. Every real project
    # ignores __pycache__, which is what makes this ordinary rather than a
    # special case -- see so-n6b.35.
    (project_dir / ".gitignore").write_text(
        ".smoke_profiling_data.json\n.downwind.json\n__pycache__/\n.smoke_suite.json\n",
    )
    _git(project_dir, "add", ".gitignore")
    _git(project_dir, "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-m", "ignore")
    return project_dir


def _selection(project_dir: Path) -> DownwindSuiteFile:
    """Read the selection back through its own schema.

    Validating rather than json.load: the file is what the pytest plugin
    reads, so a test that accepted a shape the plugin would reject would pass
    while the real run failed.
    """
    return read_downwind_suite(project_dir / ".downwind.json")


def test_editing_one_source_file_runs_only_the_tests_the_map_attributes_to_it(profiled_repo: Path) -> None:
    """The whole promise, in its ordinary case: a subset, and a justified one."""
    (profiled_repo / "src" / "app.py").write_text(
        "def add(a, b):\n    if a > 0:\n        return a + b\n    return b + 0\n",
    )

    result = _run(profiled_repo, "downwind")

    assert result.returncode == EXIT_OK, f"{result.stderr}\nSTDOUT: {result.stdout}"
    selection = _selection(profiled_repo)
    assert selection.blind_spots == []
    assert selection.changed_files == ["src/app.py"]
    assert set(selection.node_ids) == {
        "tests/test_app.py::test_add",
        "tests/test_app.py::test_add_negative",
    }
    # pytest's own accounting, not ours: the run really did deselect the rest.
    assert "2 passed" in result.stdout
    assert "1 deselected" in result.stdout


def test_a_file_the_maps_have_never_seen_runs_the_whole_suite_and_names_the_blind_spot(
    profiled_repo: Path,
) -> None:
    """New code is exactly when tests matter most, so it must never select a subset."""
    (profiled_repo / "src" / "brand_new.py").write_text("def hello():\n    return 'hi'\n")

    result = _run(profiled_repo, "downwind")

    assert result.returncode == EXIT_OK, f"{result.stderr}\nSTDOUT: {result.stdout}"
    selection = _selection(profiled_repo)
    assert BlindSpotReason.UNKNOWN_PATH in {spot.reason for spot in selection.blind_spots}
    assert "src/brand_new.py" in result.stderr
    # The whole suite, not a subset of it, and nothing deselected.
    assert "3 passed" in result.stdout
    assert "deselected" not in result.stdout
    assert selection.node_ids == []


def test_a_failing_selected_test_fails_the_command(profiled_repo: Path) -> None:
    """The command gates a commit, so a red selected test must not exit zero."""
    (profiled_repo / "src" / "app.py").write_text("def add(a, b):\n    return 0\n")

    result = _run(profiled_repo, "downwind")

    assert result.returncode == EXIT_TESTS_FAILED
    assert "failed" in result.stdout


def test_a_collection_error_never_reads_as_a_successful_selective_run(profiled_repo: Path) -> None:
    """The dangerous case: pytest cannot even import a file, and says so with a code we must not lose."""
    (profiled_repo / "src" / "app.py").write_text("import a_module_that_does_not_exist\n")

    result = _run(profiled_repo, "downwind")

    assert result.returncode != EXIT_OK


def test_running_from_a_subdirectory_errors_rather_than_selecting(profiled_repo: Path) -> None:
    """Git's paths and the profile's agree only at the root, so anywhere else must refuse."""
    result = _run(profiled_repo, "downwind", cwd=profiled_repo / "src")

    assert result.returncode == EXIT_ERROR
    assert "repository root" in result.stderr
    assert not (profiled_repo / "src" / ".downwind.json").exists()


def test_a_clean_tree_runs_nothing_and_succeeds(profiled_repo: Path) -> None:
    """Nothing changed is a real answer, and it must not fail the commit it was gating."""
    result = _run(profiled_repo, "downwind")

    assert result.returncode == EXIT_OK
    assert "nothing is downwind" in result.stdout
    # pytest exits 5 on an empty selection, so the proof it was never invoked
    # is that no pytest output appears at all.
    assert "test session starts" not in result.stdout


def test_no_profile_at_all_runs_the_full_suite_and_says_how_to_record_one(profiled_repo: Path) -> None:
    """A missing profile is an absence, not a fault: degrade to the full suite, loudly."""
    (profiled_repo / ".smoke_profiling_data.json").unlink()

    result = _run(profiled_repo, "downwind")

    assert result.returncode == EXIT_OK
    assert "no profile" in result.stderr
    assert "smoke-optimiser smoke --profile-only" in result.stderr
    assert "3 passed" in result.stdout


def test_a_corrupt_profile_errors_rather_than_quietly_running_everything(profiled_repo: Path) -> None:
    """A profile that exists and cannot be read is a broken state, and a slow run would hide it."""
    (profiled_repo / ".smoke_profiling_data.json").write_text("{not json at all")

    result = _run(profiled_repo, "downwind")

    assert result.returncode == EXIT_ERROR
    assert "Failed to parse profiling data" in result.stderr
    assert "passed" not in result.stdout


def test_the_smoke_command_still_writes_and_ranks_its_own_suite(profiled_repo: Path) -> None:
    """The coverage-per-second path's behaviour is unchanged by any of the above."""
    result = _run(profiled_repo, "smoke", "--optimise-only", "--allow-ordered", "--src=src")

    assert result.returncode == EXIT_OK, f"{result.stderr}\nSTDOUT: {result.stdout}"
    assert (profiled_repo / ".smoke_suite.json").exists()


def test_an_ordinary_smoke_run_leaves_a_profile_downwind_can_use(tmp_path: Path) -> None:
    """The default handoff: run ``smoke``, then run ``downwind``, with nothing in between.

    Deliberately not built on ``profiled_repo``, which profiles explicitly:
    the point here is that a user who never asks for ``--profile-only`` still
    ends up with a profile, so ``downwind`` selects rather than falling back to
    the whole suite.
    """
    project_dir = tmp_path / "my_project"
    _write_project(project_dir)
    (project_dir / ".gitignore").write_text(
        ".smoke_profiling_data.json\n.downwind.json\n__pycache__/\n.smoke_suite.json\n",
    )
    _git(project_dir, "init")
    _git(project_dir, "add", ".")
    _git(project_dir, "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-m", "initial")

    smoke = _run(project_dir, "smoke", "--allow-ordered", "--src=src")
    assert smoke.returncode == EXIT_OK, f"{smoke.stderr}\nSTDOUT: {smoke.stdout}"
    assert (project_dir / ".smoke_profiling_data.json").exists()

    # A real edit that keeps the test passing, so the exit code is about
    # selection rather than about the assertion inside the fixture project.
    (project_dir / "src" / "other.py").write_text(
        'def double(x):\n    """Twice x, or zero."""\n    if x > 0:\n        return x + x\n    return 0\n',
    )
    downwind = _run(project_dir, "downwind")

    assert downwind.returncode == EXIT_OK, f"{downwind.stderr}\nSTDOUT: {downwind.stdout}"
    assert "smoke-optimiser smoke --profile-only" not in downwind.stderr
    selection = _selection(project_dir)
    assert selection.blind_spots == []
    assert selection.changed_files == ["src/other.py"]
    assert set(selection.node_ids) == {"tests/test_other.py::test_double"}
