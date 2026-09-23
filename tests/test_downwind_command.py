"""The wiring's own decisions, over a real repository and a hand-built profile.

What is asserted here is what the end-to-end tests cannot arrange cheaply: a
profile whose maps say a changed file reaches no test at all, and a git that
cannot answer. Both are real repositories on disk -- only the pytest
subprocess is stubbed, since these tests are about what we decide before
pytest is reached.
"""

import json
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from smoke_optimiser.config import CovSourceOrigin, DownwindConfig, ProfilingRunConfig
from smoke_optimiser.downwind.command import run_downwind
from smoke_optimiser.paths import ProjectPaths
from smoke_optimiser.profiler.models import PROFILE_SCHEMA_VERSION
from smoke_optimiser.profiler.runner import (
    ProfilingIncompleteError,
    ProfilingRun,
    ProfilingUnavailableError,
)

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_COLLECTION_ERROR = 2


def _git(repo: Path, *args: str) -> None:
    # git comes from PATH and the arguments are this test's own literals
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)  # noqa: S603, S607


def _config(repo: Path, *, regenerate: bool = False) -> DownwindConfig:
    """A downwind config for these tests.

    Regeneration is off unless a test asks for it: most of what is exercised here
    is the selective path, where a fallback never happens, and leaving it on would
    make every full-suite assertion depend on the profiling runner as well.
    """
    return DownwindConfig(
        profile_path=repo / "profile.json",
        downwind_file_path=repo / ".downwind.json",
        pytest_args="",
        regenerate_on_fallback=regenerate,
        profiling=ProfilingRunConfig(
            cov_source="src",
            cov_source_origin=CovSourceOrigin.CONFIGURED,
            pytest_args="",
            allow_ordered=True,
            iterations=1,
        ),
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
        "schema_version": PROFILE_SCHEMA_VERSION,
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
            "iterations": 1,
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
            "include_namespace_packages": False,
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
        "anchor": {"project_offset": ".", "node_id_prefix": "."},
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


@pytest.fixture
def monorepo_with_tracked_own_artefacts(tmp_path: Path) -> Path:
    """The same early state, with the project one directory down and the paths relative.

    Relative is the point: an absolute profile_path would resolve identically
    whatever root anything joined it onto, so it could not catch the anchoring
    this fixture exists to pin. Returns the REPOSITORY root.
    """
    repo = tmp_path / "repo"
    project = repo / "api"
    (project / "src").mkdir(parents=True)
    (project / "tests").mkdir()
    (project / "src" / "a.py").write_text("from src import b\n")
    (project / "src" / "b.py").write_text("x = 1\n")
    (project / "tests" / "test_x.py").write_text("def test_x():\n    assert True\n")
    profile = _profile_where_the_change_reaches_no_test()
    profile["measured_files"] = ["api/src/a.py", "api/src/b.py"]
    profile["import_graph"]["edges"] = [
        {"importer": "api/src/b.py", "imported": "api/src/a.py"},
        {"importer": "api/src/a.py", "imported": "api/src/b.py"},
    ]
    profile["import_graph"]["unattributed_modules"] = ["api/tests/test_x.py"]
    profile["scope"] = {
        "coverage_roots": ["api/src"],
        "test_roots": ["api/tests"],
        "test_file_patterns": ["test_*.py"],
        "include_namespace_packages": False,
    }
    profile["anchor"] = {"project_offset": "api", "node_id_prefix": "api"}
    (project / "profile.json").write_text(json.dumps(profile))
    (project / ".downwind.json").write_text("{}")

    _git(repo, "init")
    _git(repo, "add", ".")
    _git(repo, "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-m", "initial")
    return repo


def test_own_artefacts_in_a_subdirectory_are_still_not_treated_as_a_change(
    monorepo_with_tracked_own_artefacts: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The artefacts are found where the project is, not where the repository starts.

    git reports them as api/profile.json while the config names profile.json, so a
    comparison that anchored the configured path on the repository root would look
    for repo/profile.json, match nothing, and read the file this tool rewrote last
    run as an unattributable change. Every run would then refuse to the full suite
    and rewrite the artefacts again -- a permanent full suite that looks like
    ordinary operation, which is exactly what it did before it was told the two
    roots apart.
    """
    project = monorepo_with_tracked_own_artefacts / "api"
    profile = _profile_where_the_change_reaches_no_test()
    profile["anchor"] = {"project_offset": "api", "node_id_prefix": "api"}
    (project / "profile.json").write_text(json.dumps(profile) + "\n")
    (project / ".downwind.json").write_text('{"rewritten": true}')

    config = replace(
        _config(project),
        profile_path=Path("profile.json"),
        downwind_file_path=Path(".downwind.json"),
    )
    with patch("smoke_optimiser.downwind.command._run_pytest") as run_pytest:
        exit_code = run_downwind(config, project)

    assert exit_code == EXIT_OK
    run_pytest.assert_not_called()
    assert "No changes in the working tree" in capsys.readouterr().out


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


def _profiling_call(run_profiling: MagicMock) -> ProfilingRunConfig:
    """The config the fallback actually handed the profiler."""
    return run_profiling.call_args.args[0]


def test_a_refusal_that_regenerates_profiles_the_full_suite_and_saves_what_it_recorded(
    repo_with_an_untested_module: Path,
) -> None:
    """The run that pays for the fallback is the run that repairs the map."""
    repo = repo_with_an_untested_module
    (repo / "src" / "new_file.py").write_text("x = 1\n")
    recorded = ProfilingRun(data=MagicMock(tests={"tests/test_a.py::test_a": object()}), returncode=EXIT_OK)

    with (
        patch("smoke_optimiser.downwind.command.run_profiling", return_value=recorded) as run_profiling,
        patch("smoke_optimiser.downwind.command.save_profile") as save,
        patch("smoke_optimiser.downwind.command._run_pytest") as run_pytest,
    ):
        exit_code = run_downwind(_config(repo, regenerate=True), repo)

    assert exit_code == EXIT_OK
    # The plain path must not also fire: one full suite, not two.
    run_pytest.assert_not_called()
    save.assert_called_once()
    assert save.call_args.args[1] == repo / "profile.json"
    assert run_profiling.call_args.args[1] == ProjectPaths(repo_root=repo, invocation_dir=repo)


def test_the_instrumented_fallback_still_carries_the_downwind_flags(
    repo_with_an_untested_module: Path,
) -> None:
    """The reason for the full suite must still reach pytest's own report header.

    Instrumenting the run is not a reason to stop saying why it is running
    everything -- the selection file is written either way, and the plugin
    reads it either way.
    """
    repo = repo_with_an_untested_module
    (repo / "src" / "new_file.py").write_text("x = 1\n")

    with (
        patch(
            "smoke_optimiser.downwind.command.run_profiling",
            return_value=ProfilingRun(data=MagicMock(tests={}), returncode=EXIT_OK),
        ) as run_profiling,
        patch("smoke_optimiser.downwind.command.save_profile"),
    ):
        run_downwind(_config(repo, regenerate=True), repo)

    assert "--downwind" in _profiling_call(run_profiling).pytest_args
    assert str(repo / ".downwind.json") in _profiling_call(run_profiling).pytest_args


def test_the_fallback_profiles_under_the_profiling_arguments_not_the_downwind_ones(
    repo_with_an_untested_module: Path,
) -> None:
    """The sharp edge this guards.

    A per-commit ``-x`` in the downwind arguments would stop the instrumented run
    at the first failure. pytest exits 1, which is a code the profiler treats as
    usable; the hook still writes every artefact; the coverage contexts still
    agree with the outcomes -- so every completeness check passes and a profile of
    a fraction of the suite lands on top of a good one. The only defence is not to
    forward them.
    """
    repo = repo_with_an_untested_module
    (repo / "src" / "new_file.py").write_text("x = 1\n")
    config = DownwindConfig(
        profile_path=repo / "profile.json",
        downwind_file_path=repo / ".downwind.json",
        pytest_args="-x --lf",
        regenerate_on_fallback=True,
        profiling=ProfilingRunConfig(
            cov_source="src",
            cov_source_origin=CovSourceOrigin.CONFIGURED,
            pytest_args="-p no:cacheprovider",
            allow_ordered=True,
            iterations=1,
        ),
    )

    with (
        patch(
            "smoke_optimiser.downwind.command.run_profiling",
            return_value=ProfilingRun(data=MagicMock(tests={}), returncode=EXIT_OK),
        ) as run_profiling,
        patch("smoke_optimiser.downwind.command.save_profile"),
    ):
        run_downwind(config, repo)

    profiling_args = _profiling_call(run_profiling).pytest_args
    assert "-p no:cacheprovider" in profiling_args
    assert "-x" not in profiling_args
    assert "--lf" not in profiling_args


def test_a_fallback_that_could_not_be_profiled_leaves_the_previous_profile_alone(
    repo_with_an_untested_module: Path,
) -> None:
    """A partial profile is worse than a stale one, so incomplete data changes nothing.

    An empty import graph does not read as "unknown", it reads as "nothing in
    this project imports anything" -- and a file nothing appears to reach selects
    no tests. The previous profile is left exactly where it was.
    """
    repo = repo_with_an_untested_module
    (repo / "src" / "new_file.py").write_text("x = 1\n")
    before = (repo / "profile.json").read_text()

    with (
        patch(
            "smoke_optimiser.downwind.command.run_profiling",
            side_effect=ProfilingIncompleteError("not all of them left every artefact behind", EXIT_OK),
        ),
        patch("smoke_optimiser.downwind.command.save_profile") as save,
    ):
        exit_code = run_downwind(_config(repo, regenerate=True), repo)

    save.assert_not_called()
    assert (repo / "profile.json").read_text() == before
    # The tests passed, so nothing else would have said the profile did not change.
    assert exit_code == EXIT_ERROR


def test_a_failed_regeneration_reports_that_the_profile_is_unchanged(
    repo_with_an_untested_module: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Saying so is the requirement: silence here is indistinguishable from success."""
    repo = repo_with_an_untested_module
    (repo / "src" / "new_file.py").write_text("x = 1\n")

    with (
        patch(
            "smoke_optimiser.downwind.command.run_profiling",
            side_effect=ProfilingIncompleteError("an xdist worker died", EXIT_OK),
        ),
        patch("smoke_optimiser.downwind.command.save_profile"),
    ):
        run_downwind(_config(repo, regenerate=True), repo)

    stderr = capsys.readouterr().err
    assert "is unchanged" in stderr
    assert "an xdist worker died" in stderr


def test_a_failing_suite_keeps_its_own_exit_code_even_when_the_profile_could_not_be_written(
    repo_with_an_untested_module: Path,
) -> None:
    """Those tests really did fail, which is truer about the commit than our own failure.

    Reporting 1 here would tell the developer their profile did not regenerate
    while hiding that their tests are red, which is the more urgent of the two.
    """
    repo = repo_with_an_untested_module
    (repo / "src" / "new_file.py").write_text("x = 1\n")

    with (
        patch(
            "smoke_optimiser.downwind.command.run_profiling",
            side_effect=ProfilingIncompleteError("pytest was interrupted", EXIT_COLLECTION_ERROR),
        ),
        patch("smoke_optimiser.downwind.command.save_profile"),
    ):
        exit_code = run_downwind(_config(repo, regenerate=True), repo)

    assert exit_code == EXIT_COLLECTION_ERROR


def test_profiling_being_unavailable_still_runs_the_tests_the_developer_was_waiting_on(
    repo_with_an_untested_module: Path,
) -> None:
    """Nothing ran, so the suite still has to. Failing to profile must not mean failing to test."""
    repo = repo_with_an_untested_module
    (repo / "src" / "new_file.py").write_text("x = 1\n")

    with (
        patch(
            "smoke_optimiser.downwind.command.run_profiling",
            side_effect=ProfilingUnavailableError("pytest not found in PATH"),
        ),
        patch("smoke_optimiser.downwind.command._run_pytest", return_value=EXIT_OK) as run_pytest,
    ):
        exit_code = run_downwind(_config(repo, regenerate=True), repo)

    assert exit_code == EXIT_OK
    assert "--downwind" in run_pytest.call_args.args[1]


def test_a_profile_that_records_no_scope_stops_the_run_rather_than_regenerating_for_ever(
    repo_with_an_untested_module: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The one fault a fallback cannot repair, so it must not be treated as one.

    Scope comes from the coverage targets and pytest's test paths, so an empty one
    names a misconfiguration rather than a stale file: profiling again produces
    another profile exactly like it. Regenerating here would mean a silent full
    suite on every commit for ever -- the ratchet this whole feature exists to end.
    """
    repo = repo_with_an_untested_module
    profile = json.loads((repo / "profile.json").read_text())
    profile["scope"] = {
        "coverage_roots": [],
        "test_roots": [],
        "test_file_patterns": [],
        "include_namespace_packages": False,
    }
    (repo / "profile.json").write_text(json.dumps(profile))

    with (
        patch("smoke_optimiser.downwind.command.run_profiling") as run_profiling,
        patch("smoke_optimiser.downwind.command._run_pytest") as run_pytest,
    ):
        exit_code = run_downwind(_config(repo, regenerate=True), repo)

    assert exit_code == EXIT_ERROR
    run_profiling.assert_not_called()
    run_pytest.assert_not_called()
    stderr = capsys.readouterr().err
    assert "no scope roots" in stderr
    assert "testpaths" in stderr


def _profile_with_coverage_roots(repo: Path, roots: list[str]) -> None:
    """Rewrite the fixture profile's scope, which is what a fallback reproduces."""
    profile = json.loads((repo / "profile.json").read_text())
    profile["scope"]["coverage_roots"] = roots
    (repo / "profile.json").write_text(json.dumps(profile))


def test_a_fallback_instruments_the_coverage_root_the_replaced_profile_used(
    repo_with_an_untested_module: Path,
) -> None:
    """A fallback must reproduce the profile it replaces, not substitute a different one.

    The failure this exists to prevent, seen in the wild: a profile recorded with
    --src=app was replaced by a fallback that re-discovered its coverage target,
    found nothing, and settled on the whole repository. That widened the scope to
    every .py file in the tree, including hook scripts coverage never measures --
    so the very next run expired the profile it had just written, for ever.
    """
    repo = repo_with_an_untested_module
    _profile_with_coverage_roots(repo, ["app"])
    (repo / "src" / "new_file.py").write_text("x = 1\n")
    # Nothing configured, so without the reuse this would fall through to a guess.
    config = _config(repo, regenerate=True)
    guessed = ProfilingRunConfig(
        cov_source=".",
        cov_source_origin=CovSourceOrigin.UNDISCOVERED,
        pytest_args="",
        allow_ordered=True,
        iterations=1,
    )
    config = replace(config, profiling=guessed)

    with (
        patch(
            "smoke_optimiser.downwind.command.run_profiling",
            return_value=ProfilingRun(data=MagicMock(tests={}), returncode=EXIT_OK),
        ) as run_profiling,
        patch("smoke_optimiser.downwind.command.save_profile"),
    ):
        run_downwind(config, repo)

    assert _profiling_call(run_profiling).cov_source == "app"
    assert _profiling_call(run_profiling).cov_source_origin is CovSourceOrigin.REPLACED_PROFILE


def test_an_explicit_coverage_source_beats_the_replaced_profiles_root(
    repo_with_an_untested_module: Path,
) -> None:
    """Reproducing the old profile is a fallback for silence, not an override of intent.

    A project that has since stated what it covers is correcting the old profile,
    so reusing its root would make that correction impossible to apply.
    """
    repo = repo_with_an_untested_module
    _profile_with_coverage_roots(repo, ["."])
    (repo / "src" / "new_file.py").write_text("x = 1\n")

    with (
        patch(
            "smoke_optimiser.downwind.command.run_profiling",
            return_value=ProfilingRun(data=MagicMock(tests={}), returncode=EXIT_OK),
        ) as run_profiling,
        patch("smoke_optimiser.downwind.command.save_profile"),
    ):
        run_downwind(_config(repo, regenerate=True), repo)

    # _config states cov_source explicitly, so the profile's "." must not win.
    assert _profiling_call(run_profiling).cov_source == "src"


def test_a_profile_with_several_coverage_roots_is_not_reduced_to_one_of_them(
    repo_with_an_untested_module: Path,
) -> None:
    """--src carries one value, and picking one of several would be its own substitution."""
    repo = repo_with_an_untested_module
    _profile_with_coverage_roots(repo, ["app", "lib"])
    (repo / "src" / "new_file.py").write_text("x = 1\n")
    config = replace(
        _config(repo, regenerate=True),
        profiling=ProfilingRunConfig(
            cov_source="guessed",
            cov_source_origin=CovSourceOrigin.SRC_LAYOUT,
            pytest_args="",
            allow_ordered=True,
            iterations=1,
        ),
    )

    with (
        patch(
            "smoke_optimiser.downwind.command.run_profiling",
            return_value=ProfilingRun(data=MagicMock(tests={}), returncode=EXIT_OK),
        ) as run_profiling,
        patch("smoke_optimiser.downwind.command.save_profile"),
    ):
        run_downwind(config, repo)

    assert _profiling_call(run_profiling).cov_source == "guessed"


def test_the_distribution_flags_carry_over_into_the_instrumented_run(
    repo_with_an_untested_module: Path,
) -> None:
    """-n and --dist change how the same tests are spread, never which tests run.

    Dropping them made the fallback run serially a suite the developer runs in
    parallel, which costs far more than the instrumentation does. The durations it
    records are contended, which `smoke` already refuses to rank without
    --allow-parallel-durations; the maps downwind selects from are unaffected.
    """
    repo = repo_with_an_untested_module
    (repo / "src" / "new_file.py").write_text("x = 1\n")
    config = replace(_config(repo, regenerate=True), pytest_args="-n auto --dist=worksteal -x -k slow")

    with (
        patch(
            "smoke_optimiser.downwind.command.run_profiling",
            return_value=ProfilingRun(data=MagicMock(tests={}), returncode=EXIT_OK),
        ) as run_profiling,
        patch("smoke_optimiser.downwind.command.save_profile"),
    ):
        run_downwind(config, repo)

    profiling_args = _profiling_call(run_profiling).pytest_args
    assert "-n auto" in profiling_args
    assert "--dist=worksteal" in profiling_args
    # The narrowing ones still must not: they would profile a fraction of the suite.
    assert "-x" not in profiling_args
    assert "-k" not in profiling_args


def test_the_kept_copy_of_an_unreadable_profile_is_not_itself_a_change(
    repo_with_an_untested_module: Path,
) -> None:
    """A fallback that repaired the map must not create the next run's blind spot.

    Rebuilding an unreadable profile leaves a .corrupt sibling behind, and a
    project's gitignore names the profile rather than its siblings -- so it turns
    up as an untracked file, which is a change the maps cannot answer for, which
    refuses and runs everything. That is the staleness ratchet again, created by
    the very run that was supposed to end it.
    """
    repo = repo_with_an_untested_module
    (repo / "profile.json.corrupt").write_text("{not json at all")

    with patch("smoke_optimiser.downwind.command._run_pytest"):
        run_downwind(_config(repo), repo)

    # Asserted against the changed set the run recorded, not against the outcome:
    # this file happens to raise no blind spot in this fixture, so a test that
    # only checked the outcome would pass whether or not it was excluded.
    changed = json.loads((repo / ".downwind.json").read_text())["changed_files"]
    assert changed == []
