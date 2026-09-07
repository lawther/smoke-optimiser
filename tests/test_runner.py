import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from smoke_optimiser.config import OperationMode, ResolvedConfig
from smoke_optimiser.profiler.runner import (
    PYTEST_HOOK_CODE,
    OutcomesIngestError,
    _read_iteration_outcomes,
    check_prerequisites,
    run_profiling,
)


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


def _write_outcomes(path: Path, worker: str | None, worker_count: int | None, node_ids: list[str]) -> None:
    """Write one outcomes file in the shape the profiling hook produces."""
    payload = {
        "worker": worker,
        "worker_count": worker_count,
        "outcomes": {node_id: {"passed": True, "duration": 0.5, "markers": ["unit"]} for node_id in node_ids},
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
        config = SimpleNamespace(
            _smoke_outcomes={f"test_{worker}": {"passed": True, "duration": 1.0, "markers": []}},
            rootpath=tmp_path,
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
    config = SimpleNamespace(
        _smoke_outcomes={"test_a": {"passed": True, "duration": 1.0, "markers": []}},
        rootpath=tmp_path,
    )
    env = {"SMOKE_OUTCOMES_JSON": str(outcomes_json)}
    with patch.dict(os.environ, env, clear=False):
        # A serial run has no worker variables at all, so they must not leak in from
        # the environment this test itself is running under, and neither must the
        # graph file of a profiling run that this test is a part of.
        os.environ.pop("PYTEST_XDIST_WORKER", None)
        os.environ.pop("PYTEST_XDIST_WORKER_COUNT", None)
        os.environ.pop("SMOKE_IMPORT_GRAPH_JSON", None)
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
