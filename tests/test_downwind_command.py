"""The wiring's own decisions, over a real repository and a hand-built profile.

What is asserted here is what the end-to-end tests cannot arrange cheaply: a
profile whose maps say a changed file reaches no test at all, and a git that
cannot answer. Both are real repositories on disk -- only the pytest
subprocess is stubbed, since these tests are about what we decide before
pytest is reached.
"""

import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from smoke_optimiser.config import DownwindConfig
from smoke_optimiser.downwind.command import run_downwind

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_COLLECTION_ERROR = 2


def _git(repo: Path, *args: str) -> None:
    # git comes from PATH and the arguments are this test's own literals
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)  # noqa: S603, S607


def _config(repo: Path) -> DownwindConfig:
    return DownwindConfig(
        profile_path=repo / "profile.json",
        downwind_file_path=repo / ".downwind.json",
        pytest_args="",
    )


def _profile_where_the_change_reaches_no_test() -> dict[str, Any]:
    """A profile whose maps can answer for src/a.py and whose answer is 'nothing'.

    src/a.py and src/b.py import each other, so each is attributed -- the
    graph is not blind to them, it genuinely records that nothing else
    depends on them. Neither defines a test and neither was executed by one,
    so the closure is real, complete, and empty. That is a selection of zero,
    not a refusal, and it is the shape this file exists to pin down.
    """
    return {
        "schema_version": 2,
        "meta": {
            "timestamp": "2026-03-02T10:30:00Z",
            "commit": "abcdef",
            "python_version": "3.12",
            "coverage_version": "7.0",
            "command": "smoke-optimiser",
            "machine": {
                "os": "Linux",
                "os_version": "6.5",
                "platform": "Ubuntu",
                "architecture": "x86_64",
                "cpu_model": "AMD",
                "cpu_cores_physical": 16,
                "cpu_cores_logical": 32,
                "ram_total_mb": 65536,
                "ram_available_mb": 58200,
                "hostname": "ci-04",
            },
            "xdist_workers": 1,
        },
        "tests": {
            "tests/test_x.py::test_x": {
                "test_id": "tests/test_x.py::test_x",
                "duration_s": 0.1,
                "passed": True,
                "branches_covered": [],
                "files_covered": [],
                "markers": [],
            },
        },
        "total_branches": [],
        "measured_files": ["src/a.py", "src/b.py"],
        "scope": {
            "coverage_roots": ["src"],
            "test_roots": ["tests"],
            "test_file_patterns": ["test_*.py"],
        },
        "import_graph": {
            "edges": [
                {"importer": "src/b.py", "imported": "src/a.py"},
                {"importer": "src/a.py", "imported": "src/b.py"},
            ],
            "unattributed_modules": ["tests/test_x.py"],
            "resolution_errors": 0,
            "error_samples": [],
        },
    }


@pytest.fixture
def repo_with_an_untested_module(tmp_path: Path) -> Path:
    """A repository whose tracked tree matches the profile above exactly.

    Every tracked .py file in scope must be one the maps know, or expiry
    fires and the answer becomes a refusal rather than the empty selection
    this fixture is for.
    """
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "src" / "a.py").write_text("from src import b\n")
    (repo / "src" / "b.py").write_text("x = 1\n")
    (repo / "tests" / "test_x.py").write_text("def test_x():\n    assert True\n")
    (repo / "profile.json").write_text(json.dumps(_profile_where_the_change_reaches_no_test()))
    (repo / ".gitignore").write_text("profile.json\n.downwind.json\n")

    _git(repo, "init")
    _git(repo, "add", ".")
    _git(repo, "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-m", "initial")
    return repo


def test_a_change_no_test_reaches_runs_nothing_and_says_which_files(
    repo_with_an_untested_module: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Zero tests for a real change is a finding, not a silence.

    It must not fail the commit -- the answer is correct -- but it must name
    the files and say how to rebuild the profile, because the other thing it
    can mean is that the profile is missing a route to the suite.
    """
    repo = repo_with_an_untested_module
    (repo / "src" / "a.py").write_text("from src import b\n\nY = 2\n")

    with patch("smoke_optimiser.downwind.command._run_pytest") as run_pytest:
        exit_code = run_downwind(_config(repo), repo)

    assert exit_code == EXIT_OK
    run_pytest.assert_not_called()

    captured = capsys.readouterr()
    assert "no test reaches them" in captured.err
    assert "src/a.py" in captured.err
    assert "smoke-optimiser smoke --profile-only" in captured.err
    assert "pytest not run" in captured.out

    # The answer is still written down, so a zero can be inspected rather than
    # taken on trust.
    with (repo / ".downwind.json").open("rb") as f:
        selection = json.load(f)
    assert selection["node_ids"] == []
    assert selection["blind_spots"] == []
    assert selection["changed_files"] == ["src/a.py"]


@pytest.fixture
def repo_with_tracked_own_artefacts(tmp_path: Path) -> Path:
    """The profile and selection file are tracked, not gitignored -- the common early state.

    Nothing here changes them after the commit; the test that uses this fixture does that, so
    that the resulting git status is one this tool caused, not one a developer caused.
    """
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "src" / "a.py").write_text("from src import b\n")
    (repo / "src" / "b.py").write_text("x = 1\n")
    (repo / "tests" / "test_x.py").write_text("def test_x():\n    assert True\n")
    (repo / "profile.json").write_text(json.dumps(_profile_where_the_change_reaches_no_test()))
    (repo / ".downwind.json").write_text("{}")

    _git(repo, "init")
    _git(repo, "add", ".")
    _git(repo, "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-m", "initial")
    return repo


def test_rewriting_its_own_artefacts_is_not_treated_as_a_change(
    repo_with_tracked_own_artefacts: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A project that has not gitignored the profile and selection file must not be punished for it.

    Both files are rewritten by this tool's own previous runs, not by the developer -- so seeing
    them dirty in git status must not force the full suite the way a real, unattributable change
    would.
    """
    repo = repo_with_tracked_own_artefacts
    (repo / "profile.json").write_text(json.dumps(_profile_where_the_change_reaches_no_test()) + "\n")
    (repo / ".downwind.json").write_text('{"rewritten": true}')

    with patch("smoke_optimiser.downwind.command._run_pytest") as run_pytest:
        exit_code = run_downwind(_config(repo), repo)

    assert exit_code == EXIT_OK
    run_pytest.assert_not_called()
    assert "No changes in the working tree" in capsys.readouterr().out

    with (repo / ".downwind.json").open("rb") as f:
        selection = json.load(f)
    assert selection["changed_files"] == []
    assert selection["blind_spots"] == []


def test_a_clean_tree_says_nothing_changed_rather_than_warning_about_untested_files(
    repo_with_an_untested_module: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The other empty case, and it must not be dressed up as a finding."""
    with patch("smoke_optimiser.downwind.command._run_pytest") as run_pytest:
        exit_code = run_downwind(_config(repo_with_an_untested_module), repo_with_an_untested_module)

    assert exit_code == EXIT_OK
    run_pytest.assert_not_called()

    captured = capsys.readouterr()
    assert "No changes in the working tree" in captured.out
    assert "no test reaches them" not in captured.err


def test_git_being_unavailable_names_the_command_the_directory_and_what_git_said(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A diagnostic a user can act on, not a bare exception class name.

    git's own stderr is reproduced because it distinguishes 'not a
    repository' from a broken index or a permissions problem far better than
    anything this layer could infer from a return code.
    """
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()

    exit_code = run_downwind(_config(not_a_repo), not_a_repo)

    assert exit_code == EXIT_ERROR
    stderr = capsys.readouterr().err
    assert "git rev-parse --show-toplevel" in stderr
    assert str(not_a_repo) in stderr
    assert "not a git repository" in stderr


def test_a_failing_pytest_exit_code_is_passed_through_untouched(
    repo_with_an_untested_module: Path,
) -> None:
    """The command gates a commit, so nothing may reinterpret pytest's verdict.

    Exit 2 is the collection-error code: a run where pytest imported nothing
    and proved nothing, which must never read as a successful selective run.
    """
    repo = repo_with_an_untested_module
    (repo / "src" / "new_file.py").write_text("x = 1\n")

    with patch("smoke_optimiser.downwind.command._run_pytest", return_value=EXIT_COLLECTION_ERROR):
        exit_code = run_downwind(_config(repo), repo)

    assert exit_code == EXIT_COLLECTION_ERROR


def test_a_refusal_still_runs_pytest_under_downwind_so_the_reason_reaches_its_header(
    repo_with_an_untested_module: Path,
) -> None:
    """One invocation path for both outcomes.

    The selection file carries the blind spots and no node ids, the plugin
    leaves collection untouched, and pytest's own report header states the
    reason -- so it travels with the run rather than only with our console
    output.
    """
    repo = repo_with_an_untested_module
    (repo / "src" / "new_file.py").write_text("x = 1\n")

    with patch("smoke_optimiser.downwind.command._run_pytest", return_value=0) as run_pytest:
        run_downwind(_config(repo), repo)

    pytest_args = run_pytest.call_args.args[1]
    assert "--downwind" in pytest_args
    assert f"--downwind-file-path={repo / '.downwind.json'}" in pytest_args

    with (repo / ".downwind.json").open("rb") as f:
        selection = json.load(f)
    assert selection["node_ids"] == []
    # Just the one reason: the file is untracked, so the tree enumeration does
    # not see it and expiry never fires for it. That is the point of listing
    # tracked files only -- a file you just created is answered by the more
    # accurate "the maps have never seen this path".
    assert [spot["reason"] for spot in selection["blind_spots"]] == ["unknown_path"]


def test_a_tracked_file_the_maps_never_saw_expires_the_profile(
    repo_with_an_untested_module: Path,
) -> None:
    """The other half of the same rule: once it is tracked, it is part of the tree.

    A file a teammate added and committed is in scope and unknown to the
    maps, so every answer they give omits whatever it contains -- which is
    divergence, and expires the profile whether or not it is what you
    changed.
    """
    repo = repo_with_an_untested_module
    (repo / "src" / "theirs.py").write_text("x = 1\n")
    _git(repo, "add", "src/theirs.py")
    _git(repo, "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-m", "theirs")
    # Change something else entirely: expiry is a fact about the tree, not
    # about what this commit touched.
    (repo / "src" / "a.py").write_text("from src import b\n\nY = 2\n")

    with patch("smoke_optimiser.downwind.command._run_pytest", return_value=0):
        run_downwind(_config(repo), repo)

    with (repo / ".downwind.json").open("rb") as f:
        selection = json.load(f)
    assert [(spot["reason"], spot["file"]) for spot in selection["blind_spots"]] == [
        ("expired_profile", "src/theirs.py"),
    ]
    assert selection["changed_files"] == ["src/a.py"]
