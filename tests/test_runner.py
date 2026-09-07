import json
import os
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from smoke_optimiser.config import OperationMode, ResolvedConfig
from smoke_optimiser.profiler.runner import (
    PYTEST_HOOK_CODE,
    IterationOutcomes,
    OutcomesIngestError,
    _read_iteration_outcomes,
    _warn_about_an_unbounded_test_scope,
    check_prerequisites,
    run_profiling,
)
from smoke_optimiser.profiler.scope import ProfileScope


def test_check_prerequisites_success() -> None:
    config = ResolvedConfig(
        mode=OperationMode.FULL,
        time_cap=15.0,
        target_cov=100.0,
        include_mandatory=[],
        exclude_mandatory=[],
        pytest_args="",
        output_json=Path(".json"),
        allow_ordered=True,
        cov_source=".",
        iterations=1,
        allow_parallel_durations=False,
    )
    with patch("shutil.which", return_value="/usr/bin/pytest"):
        check_prerequisites(config)


@patch("subprocess.run")
@patch("smoke_optimiser.profiler.runner.build_profiling_data")
def test_run_profiling_basic(mock_ingest: MagicMock, mock_run: MagicMock, tmp_path: Path) -> None:
    config = ResolvedConfig(
        mode=OperationMode.FULL,
        time_cap=15.0,
        target_cov=100.0,
        include_mandatory=[],
        exclude_mandatory=[],
        pytest_args="",
        output_json=Path(".json"),
        allow_ordered=True,
        cov_source=".",
        iterations=1,
        allow_parallel_durations=False,
    )

    mock_run.return_value = MagicMock(returncode=0, stdout="pytest-randomly")
    mock_ingest.return_value = MagicMock()

    with patch("shutil.which", return_value="/usr/bin/pytest"):
        run_profiling(config, tmp_path)

    # Verify pytest command
    args, _kwargs = mock_run.call_args_list[0]
    cmd = args[0]
    assert "-m" in cmd
    assert "pytest" in cmd
    # We check that some --cov is present
    assert any(arg.startswith("--cov") for arg in cmd)
    assert "-p" in cmd
    # Contexts are read from the coverage database, so no JSON export is run.
    assert not any("json" in str(call.args[0]) for call in mock_run.call_args_list)
    assert "--cov-context=test" in cmd


@patch("subprocess.run")
@patch("smoke_optimiser.profiler.runner.build_profiling_data")
def test_cov_report_in_pytest_args_does_not_suppress_the_cov_source(
    mock_ingest: MagicMock,
    mock_run: MagicMock,
    tmp_path: Path,
) -> None:
    """--cov-report is not --cov, and must not be mistaken for it.

    Matching any argument starting with '--cov' treated --cov-report, --cov-branch
    and --cov-config as though the user had chosen what to measure. The source
    restriction was then dropped, so coverage measured everything imported --
    site-packages included -- which both inflates total_branches and makes
    measured_files describe the wrong tree.
    """
    config = ResolvedConfig(
        mode=OperationMode.FULL,
        time_cap=15.0,
        target_cov=100.0,
        include_mandatory=[],
        exclude_mandatory=[],
        pytest_args="--cov-report=term",
        output_json=Path(".json"),
        allow_ordered=True,
        cov_source="smoke_optimiser",
        iterations=1,
        allow_parallel_durations=False,
    )

    mock_run.return_value = MagicMock(returncode=0, stdout="")
    mock_ingest.return_value = MagicMock()

    with patch("shutil.which", return_value="/usr/bin/pytest"):
        run_profiling(config, tmp_path)

    cmd = mock_run.call_args_list[0].args[0]
    assert "--cov=smoke_optimiser" in cmd


@patch("subprocess.run")
@patch("smoke_optimiser.profiler.runner.build_profiling_data")
def test_an_explicit_cov_source_is_left_alone(mock_ingest: MagicMock, mock_run: MagicMock, tmp_path: Path) -> None:
    config = ResolvedConfig(
        mode=OperationMode.FULL,
        time_cap=15.0,
        target_cov=100.0,
        include_mandatory=[],
        exclude_mandatory=[],
        pytest_args="--cov=chosen_package",
        output_json=Path(".json"),
        allow_ordered=True,
        cov_source="smoke_optimiser",
        iterations=1,
        allow_parallel_durations=False,
    )

    mock_run.return_value = MagicMock(returncode=0, stdout="")
    mock_ingest.return_value = MagicMock()

    with patch("shutil.which", return_value="/usr/bin/pytest"):
        run_profiling(config, tmp_path)

    cmd = mock_run.call_args_list[0].args[0]
    assert "--cov=chosen_package" in cmd
    assert "--cov=smoke_optimiser" not in cmd


THREE_WORKERS = 3
TWO_WORKERS = 2


def _fake_pytest_config(project_root: Path, outcomes: dict[str, Any] | None = None) -> SimpleNamespace:
    """Stand in for the pytest Config the hook is handed inside the subprocess.

    Only the attributes the hook reads: the outcomes it accumulated, the resolved
    --cov sources, the positional arguments after testpaths were applied, the
    collection patterns, and where pytest was invoked from.
    """
    return SimpleNamespace(
        _smoke_outcomes=outcomes if outcomes is not None else {},
        rootpath=project_root,
        args=["tests"],
        option=SimpleNamespace(cov_source=["src"]),
        getini=lambda name: ["test_*.py"] if name == "python_files" else [],
        invocation_params=SimpleNamespace(dir=project_root),
    )


def _write_outcomes(
    path: Path,
    worker: str | None,
    worker_count: int | None,
    node_ids: list[str],
    collection_errors: list[str] | None = None,
) -> None:
    """Write one outcomes file in the shape the profiling hook produces."""
    payload = {
        "worker": worker,
        "worker_count": worker_count,
        "outcomes": {node_id: {"passed": True, "duration": 0.5, "markers": ["unit"]} for node_id in node_ids},
        "collection_errors": collection_errors or [],
        "scope": {"coverage_roots": ["src"], "test_roots": ["tests"], "test_file_patterns": ["test_*.py"]},
    }
    path.write_text(json.dumps(payload))


def test_serial_run_reads_the_single_outcomes_file(tmp_path: Path) -> None:
    outcomes_json = tmp_path / "outcomes.json"
    _write_outcomes(outcomes_json, None, None, ["test_a"])

    iteration = _read_iteration_outcomes(outcomes_json)

    assert set(iteration.outcomes) == {"test_a"}
    assert iteration.xdist_workers == 1
    # The file is consumed so the next iteration cannot re-read this one's results.
    assert not outcomes_json.exists()


def test_every_xdist_worker_s_outcomes_survive_the_merge(tmp_path: Path) -> None:
    """The bug this guards: workers sharing one file left only the last writer's tests.

    Each worker runs a disjoint slice of the suite, so dropping a worker's file drops
    those tests' durations, outcomes and markers entirely -- silently, since the run
    still succeeds and simply profiles less of the suite than it claims to.
    """
    outcomes_json = tmp_path / "outcomes.json"
    # The controller process writes the unsuffixed file, and runs no tests itself.
    _write_outcomes(outcomes_json, None, None, [])
    _write_outcomes(tmp_path / "outcomes.gw0.json", "gw0", THREE_WORKERS, ["test_a", "test_b"])
    _write_outcomes(tmp_path / "outcomes.gw1.json", "gw1", THREE_WORKERS, ["test_c"])
    _write_outcomes(tmp_path / "outcomes.gw2.json", "gw2", THREE_WORKERS, ["test_d"])

    iteration = _read_iteration_outcomes(outcomes_json)

    assert set(iteration.outcomes) == {"test_a", "test_b", "test_c", "test_d"}
    assert iteration.xdist_workers == THREE_WORKERS
    assert list(tmp_path.glob("outcomes*.json")) == []


def test_an_unreadable_outcomes_file_is_an_error_not_a_partial_profile(tmp_path: Path) -> None:
    """A half-read outcomes file would profile part of the suite while looking fine."""
    outcomes_json = tmp_path / "outcomes.json"
    outcomes_json.write_text('{"worker": null, "outcomes": "not a mapping"}')

    with pytest.raises(OutcomesIngestError):
        _read_iteration_outcomes(outcomes_json)


def test_the_profiling_hook_gives_each_xdist_worker_its_own_file(tmp_path: Path) -> None:
    """Exercise the hook source itself, since it only ever runs in a subprocess.

    Executing the plugin string here is the only way to check that the filename it
    chooses actually varies by worker -- an end-to-end run would need xdist installed,
    and would still pass if every worker wrote the same path, because the last writer
    produces a perfectly valid file.
    """
    namespace: dict[str, Any] = {}
    exec(PYTEST_HOOK_CODE, namespace)  # noqa: S102 - the hook is only ever a string, so it must be exec'd to be tested

    outcomes_json = tmp_path / "outcomes.json"

    for worker in ("gw0", "gw1"):
        config = _fake_pytest_config(
            tmp_path,
            outcomes={f"test_{worker}": {"passed": True, "duration": 1.0, "markers": []}},
        )
        env = {
            "SMOKE_OUTCOMES_JSON": str(outcomes_json),
            "PYTEST_XDIST_WORKER": worker,
            "PYTEST_XDIST_WORKER_COUNT": "2",
        }
        with patch.dict(os.environ, env, clear=False):
            # This test's own process may be a profiling run, whose graph file the
            # exec'd hook would otherwise write into.
            os.environ.pop("SMOKE_IMPORT_GRAPH_JSON", None)
            namespace["pytest_unconfigure"](config)

    iteration = _read_iteration_outcomes(outcomes_json)

    assert set(iteration.outcomes) == {"test_gw0", "test_gw1"}
    assert iteration.xdist_workers == TWO_WORKERS


def test_the_profiling_hook_keeps_the_plain_filename_when_serial(tmp_path: Path) -> None:
    namespace: dict[str, Any] = {}
    exec(PYTEST_HOOK_CODE, namespace)  # noqa: S102 - the hook is only ever a string, so it must be exec'd to be tested

    outcomes_json = tmp_path / "outcomes.json"
    config = _fake_pytest_config(tmp_path, outcomes={"test_a": {"passed": True, "duration": 1.0, "markers": []}})
    env = {"SMOKE_OUTCOMES_JSON": str(outcomes_json)}
    with patch.dict(os.environ, env, clear=False):
        # A serial run has no worker variables at all, so they must not leak in from
        # the environment this test itself is running under, and neither must the
        # graph file of a profiling run that this test is a part of.
        os.environ.pop("PYTEST_XDIST_WORKER", None)
        os.environ.pop("PYTEST_XDIST_WORKER_COUNT", None)
        os.environ.pop("SMOKE_IMPORT_GRAPH_JSON", None)
        os.environ.pop("SMOKE_PROJECT_ROOT", None)
        namespace["pytest_unconfigure"](config)

    assert outcomes_json.exists()
    assert _read_iteration_outcomes(outcomes_json).xdist_workers == 1


@patch("subprocess.run")
@patch("smoke_optimiser.profiler.runner.build_profiling_data")
def test_an_outer_xdist_worker_does_not_leak_into_the_profiled_run(
    mock_ingest: MagicMock,
    mock_run: MagicMock,
    tmp_path: Path,
) -> None:
    """smoke-optimiser can be invoked from inside someone else's xdist worker.

    Its own worker variables are in the environment then, and would be inherited by
    the serial pytest we launch -- which would report the profiled suite as parallel
    and refuse to rank it, purely because of who called us.
    """
    config = ResolvedConfig(
        mode=OperationMode.FULL,
        time_cap=15.0,
        target_cov=100.0,
        include_mandatory=[],
        exclude_mandatory=[],
        pytest_args="",
        output_json=Path(".json"),
        allow_ordered=True,
        cov_source="smoke_optimiser",
        iterations=1,
        allow_parallel_durations=False,
    )
    outer = {"PYTEST_XDIST_WORKER": "gw0", "PYTEST_XDIST_WORKER_COUNT": "4"}

    mock_run.return_value = MagicMock(returncode=0, stdout="pytest-randomly")
    mock_ingest.return_value = MagicMock()

    with patch.dict(os.environ, outer, clear=False), patch("shutil.which", return_value="/usr/bin/pytest"):
        run_profiling(config, tmp_path)

    # subprocess.run is also used for git, so pick out the call that launched pytest.
    pytest_envs = [call.kwargs["env"] for call in mock_run.call_args_list if "env" in call.kwargs]
    assert pytest_envs
    for child_env in pytest_envs:
        assert "PYTEST_XDIST_WORKER" not in child_env
        assert "PYTEST_XDIST_WORKER_COUNT" not in child_env


INTERRUPTED = 2
NO_TESTS_COLLECTED = 5
KILLED_BY_SIGNAL = 137
THREE_ITERATIONS = 3
PYTEST_LAUNCHES = 2


def _profiling_config(iterations: int = 1) -> ResolvedConfig:
    """A configuration for a profiling run, with only what a test cares about spelled out."""
    return ResolvedConfig(
        mode=OperationMode.FULL,
        time_cap=15.0,
        target_cov=100.0,
        include_mandatory=[],
        exclude_mandatory=[],
        pytest_args="",
        output_json=Path(".json"),
        allow_ordered=True,
        cov_source=".",
        iterations=iterations,
        allow_parallel_durations=False,
    )


def _pytest_exit_codes(*codes: int) -> Callable[..., MagicMock]:
    """A subprocess.run stand-in giving each pytest launch the next exit code.

    subprocess.run also serves the git commit lookup, which must not eat one of the
    codes, so the pytest calls are picked out by their command line.
    """
    remaining = list(codes)

    def run(cmd: list[str], **_kwargs: object) -> MagicMock:
        if "pytest" in cmd:
            return MagicMock(returncode=remaining.pop(0), stdout="")
        return MagicMock(returncode=0, stdout="")

    return run


@patch("subprocess.run")
@patch("smoke_optimiser.profiler.runner.build_profiling_data")
def test_a_pytest_run_that_did_not_finish_produces_no_profile(
    mock_ingest: MagicMock,
    mock_run: MagicMock,
    tmp_path: Path,
) -> None:
    """Exit code 2 means pytest stopped early, so the suite it measured is a subset.

    The return code used to be discarded entirely, and profiling proceeded on whatever
    coverage the abandoned run had already written -- a smoke suite that under-selects
    with nothing to say it did.
    """
    mock_run.side_effect = _pytest_exit_codes(INTERRUPTED)

    with patch("shutil.which", return_value="/usr/bin/pytest"), pytest.raises(SystemExit):
        run_profiling(_profiling_config(), tmp_path)

    mock_ingest.assert_not_called()


@patch("subprocess.run")
@patch("smoke_optimiser.profiler.runner.build_profiling_data")
def test_collecting_no_tests_says_so_rather_than_blaming_the_database(
    mock_ingest: MagicMock,
    mock_run: MagicMock,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Exit 5 was caught only by luck downstream, as a missing coverage database."""
    mock_run.side_effect = _pytest_exit_codes(NO_TESTS_COLLECTED)

    with patch("shutil.which", return_value="/usr/bin/pytest"), pytest.raises(SystemExit):
        run_profiling(_profiling_config(), tmp_path)

    assert "collected no tests" in capsys.readouterr().err
    mock_ingest.assert_not_called()


@patch("subprocess.run")
@patch("smoke_optimiser.profiler.runner.build_profiling_data")
def test_an_exit_code_pytest_does_not_define_is_still_fatal(
    mock_ingest: MagicMock,
    mock_run: MagicMock,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A killed pytest reports its signal, which is in no exit code table."""
    mock_run.side_effect = _pytest_exit_codes(KILLED_BY_SIGNAL)

    with patch("shutil.which", return_value="/usr/bin/pytest"), pytest.raises(SystemExit):
        run_profiling(_profiling_config(), tmp_path)

    assert str(KILLED_BY_SIGNAL) in capsys.readouterr().err
    mock_ingest.assert_not_called()


@patch("subprocess.run")
@patch("smoke_optimiser.profiler.runner.build_profiling_data")
def test_failing_tests_do_not_stop_the_run(mock_ingest: MagicMock, mock_run: MagicMock, tmp_path: Path) -> None:
    """Exit code 1 is the ordinary outcome of a suite with failures, which is profiled."""
    mock_run.side_effect = _pytest_exit_codes(1)

    with patch("shutil.which", return_value="/usr/bin/pytest"):
        run_profiling(_profiling_config(), tmp_path)

    mock_ingest.assert_called_once()


@patch("subprocess.run")
@patch("smoke_optimiser.profiler.runner.build_profiling_data")
def test_a_later_iteration_that_did_not_finish_keeps_the_earlier_ones(
    mock_ingest: MagicMock,
    mock_run: MagicMock,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Coverage accumulates into one database, so the completed iterations still stand.

    Only the abandoned iteration's durations are unusable, since the tests it never
    reached would otherwise be averaged over fewer samples than the rest.
    """
    mock_run.side_effect = _pytest_exit_codes(0, INTERRUPTED)

    with patch("shutil.which", return_value="/usr/bin/pytest"):
        run_profiling(_profiling_config(iterations=THREE_ITERATIONS), tmp_path)

    # The third iteration is never launched: the run stops at the one that failed.
    assert len([call for call in mock_run.call_args_list if "pytest" in call.args[0]]) == PYTEST_LAUNCHES
    assert "did not finish" in capsys.readouterr().err
    mock_ingest.assert_called_once()


@patch("subprocess.run")
@patch("smoke_optimiser.profiler.runner.build_profiling_data")
def test_a_collection_error_is_fatal_even_when_pytest_exits_zero(
    mock_ingest: MagicMock,
    mock_run: MagicMock,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The exit code cannot be trusted to reveal a collection error.

    --continue-on-collection-errors turns one into exit 1, indistinguishable from
    ordinary test failures, and a project can set it in its own addopts where no flag
    smoke-optimiser sees ever mentions it. What the hook recorded is the only signal.
    """
    mock_run.side_effect = _pytest_exit_codes(0)
    outcomes = IterationOutcomes(
        outcomes={},
        xdist_workers=1,
        collection_errors=frozenset({"tests/test_broken.py"}),
        scope=ProfileScope(
            coverage_roots=frozenset({"src"}),
            test_roots=frozenset({"tests"}),
            test_file_patterns=("test_*.py",),
        ),
    )

    with (
        patch("shutil.which", return_value="/usr/bin/pytest"),
        patch("smoke_optimiser.profiler.runner._read_iteration_outcomes", return_value=outcomes),
        pytest.raises(SystemExit),
    ):
        run_profiling(_profiling_config(), tmp_path)

    assert "tests/test_broken.py" in capsys.readouterr().err
    mock_ingest.assert_not_called()


def test_the_profiling_hook_records_files_it_could_not_collect(tmp_path: Path) -> None:
    """Exercise the hook source itself, since it only ever runs in a subprocess."""
    namespace: dict[str, Any] = {}
    exec(PYTEST_HOOK_CODE, namespace)  # noqa: S102 - the hook is only ever a string, so it must be exec'd to be tested

    namespace["pytest_collectreport"](SimpleNamespace(failed=True, nodeid="tests/test_broken.py"))
    namespace["pytest_collectreport"](SimpleNamespace(failed=False, nodeid="tests/test_fine.py"))

    outcomes_json = tmp_path / "outcomes.json"
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    config = _fake_pytest_config(tmp_path)
    with patch.dict(os.environ, {"SMOKE_OUTCOMES_JSON": str(outcomes_json)}, clear=False):
        os.environ.pop("PYTEST_XDIST_WORKER", None)
        os.environ.pop("PYTEST_XDIST_WORKER_COUNT", None)
        # This test's own process may be a profiling run, whose graph file the exec'd
        # hook would otherwise write into.
        os.environ.pop("SMOKE_IMPORT_GRAPH_JSON", None)
        os.environ.pop("SMOKE_PROJECT_ROOT", None)
        namespace["pytest_unconfigure"](config)

    assert _read_iteration_outcomes(outcomes_json).collection_errors == frozenset({"tests/test_broken.py"})


def test_the_profiling_hook_records_the_scope_the_run_resolved(tmp_path: Path) -> None:
    """The scope must come from the child process, where addopts have been applied.

    The runner builds a command line that never mentions testpaths, and a project's
    own addopts can add a --cov it never sees, so anything read outside this process
    would disagree with what was actually measured.
    """
    namespace: dict[str, Any] = {}
    exec(PYTEST_HOOK_CODE, namespace)  # noqa: S102 - the hook is only ever a string, so it must be exec'd to be tested

    outcomes_json = tmp_path / "outcomes.json"
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    config = _fake_pytest_config(tmp_path)
    with patch.dict(os.environ, {"SMOKE_OUTCOMES_JSON": str(outcomes_json)}, clear=False):
        os.environ.pop("PYTEST_XDIST_WORKER", None)
        os.environ.pop("PYTEST_XDIST_WORKER_COUNT", None)
        os.environ.pop("SMOKE_IMPORT_GRAPH_JSON", None)
        os.environ.pop("SMOKE_PROJECT_ROOT", None)
        namespace["pytest_unconfigure"](config)

    scope = _read_iteration_outcomes(outcomes_json).scope
    assert scope.coverage_roots == frozenset({"src"})
    assert scope.test_roots == frozenset({"tests"})


@pytest.mark.parametrize(
    ("test_roots", "expect_warning"),
    [(frozenset({"."}), True), (frozenset({"tests"}), False)],
)
def test_an_unbounded_test_scope_is_warned_about_with_the_steps_that_fix_it(
    test_roots: frozenset[str],
    expect_warning: bool,  # noqa: FBT001 - the parametrised expectation, not a mode switch
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A profile whose test root is the repository root carries a narrower guarantee.

    It still works, so this is a warning rather than a refusal -- but the user has
    no way to discover what they lost, and the steps that widen it are specific
    enough to state outright.
    """
    scope = ProfileScope(
        coverage_roots=frozenset({"src"}),
        test_roots=test_roots,
        test_file_patterns=("test_*.py",),
    )

    _warn_about_an_unbounded_test_scope(scope)

    stderr = capsys.readouterr().err
    assert ("no configured test paths" in stderr) is expect_warning
    if expect_warning:
        assert "Create a tests/ directory" in stderr
        assert 'testpaths = ["tests"]' in stderr
        assert "Re-run smoke-optimiser" in stderr
