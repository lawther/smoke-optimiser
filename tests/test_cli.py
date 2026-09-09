import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import typer
from typer.testing import CliRunner

from smoke_optimiser.cli import _reject_parallel_durations, app
from smoke_optimiser.config import CovSourceOrigin, OperationMode, ProfilingRunConfig, ResolvedConfig
from smoke_optimiser.environment import MachineEnvironment
from smoke_optimiser.profiler.models import PROFILE_SCHEMA_VERSION, ImportGraph, ProfilingData, ProfilingMeta
from smoke_optimiser.profiler.persistence import load_profile
from smoke_optimiser.profiler.runner import ProfilingRun
from smoke_optimiser.profiler.scope import ProfileScope
from tests.conftest import RawProfileFactory

runner = CliRunner()

EXIT_CODE_SUCCESS = 0
EXIT_CODE_ERROR = 1

TIME_CAP_VALUE = 45.0
TARGET_COV_VALUE = 80.0


@patch("smoke_optimiser.cli.run_profiling", new=MagicMock())
@patch("smoke_optimiser.cli.optimise", new=MagicMock())
@patch("smoke_optimiser.cli.write_smoke_suite", new=MagicMock())
@patch("smoke_optimiser.cli.format_summary", new=MagicMock())
def test_cli_help() -> None:
    """Both selection modes must be reachable, and be named the way every message names them."""
    result = runner.invoke(app, ["--help"], env={"COLUMNS": "200"})
    assert result.exit_code == EXIT_CODE_SUCCESS
    assert "smoke" in result.stdout
    assert "downwind" in result.stdout

    smoke_help = runner.invoke(app, ["smoke", "--help"], env={"COLUMNS": "200"})
    assert smoke_help.exit_code == EXIT_CODE_SUCCESS
    assert "--time-cap" in smoke_help.stdout

    downwind_help = runner.invoke(app, ["downwind", "--help"], env={"COLUMNS": "200"})
    assert downwind_help.exit_code == EXIT_CODE_SUCCESS
    # The optimiser's options must not have leaked onto a command that cannot honour them.
    assert "--time-cap" not in downwind_help.stdout


@patch("smoke_optimiser.cli.run_profiling")
@patch("smoke_optimiser.cli.optimise")
@patch("smoke_optimiser.cli.write_smoke_suite", new=MagicMock())
@patch("smoke_optimiser.cli.format_summary")
def test_cli_defaults(
    mock_format: MagicMock,
    mock_optimise: MagicMock,
    mock_run: MagicMock,
    profiled_suite: ProfilingData,
    tmp_path: Path,
) -> None:
    mock_run.return_value = ProfilingRun(data=profiled_suite, returncode=0)
    mock_optimise.return_value = MagicMock()
    mock_format.return_value = "Summary"

    with patch("pathlib.Path.cwd", return_value=tmp_path):
        result = runner.invoke(app, ["smoke"])
    assert result.exit_code == EXIT_CODE_SUCCESS


@patch("smoke_optimiser.cli.run_profiling")
@patch("smoke_optimiser.cli.optimise")
@patch("smoke_optimiser.cli.write_smoke_suite", new=MagicMock())
@patch("smoke_optimiser.cli.format_summary")
def test_cli_overrides(
    mock_format: MagicMock,
    mock_optimise: MagicMock,
    mock_run: MagicMock,
    profiled_suite: ProfilingData,
    tmp_path: Path,
) -> None:
    mock_run.return_value = ProfilingRun(data=profiled_suite, returncode=0)
    mock_optimise.return_value = MagicMock()
    mock_format.return_value = "Summary"

    with patch("pathlib.Path.cwd", return_value=tmp_path):
        result = runner.invoke(
            app,
            ["smoke", "--time-cap", str(TIME_CAP_VALUE), "--target-cov", str(TARGET_COV_VALUE)],
        )
    assert result.exit_code == EXIT_CODE_SUCCESS


@patch("smoke_optimiser.cli.run_profiling")
@patch("smoke_optimiser.cli.optimise", new=MagicMock())
@patch("smoke_optimiser.cli.write_smoke_suite", new=MagicMock())
@patch("smoke_optimiser.cli.format_summary", new=MagicMock())
def test_cli_profile_only(
    mock_run: MagicMock,
    profiled_suite: ProfilingData,
    tmp_path: Path,
) -> None:
    mock_run.return_value = ProfilingRun(data=profiled_suite, returncode=0)

    with patch("pathlib.Path.cwd", return_value=tmp_path):
        result = runner.invoke(app, ["smoke", "--profile-only"])
    assert result.exit_code == EXIT_CODE_SUCCESS


def test_cli_mutually_exclusive() -> None:
    result = runner.invoke(app, ["smoke", "--profile-only", "--optimise-only"])
    assert result.exit_code == EXIT_CODE_ERROR
    assert "❌ Error: --profile-only and --optimise-only are mutually exclusive." in result.stderr


@patch("smoke_optimiser.cli.run_profiling")
@patch("smoke_optimiser.cli.optimise")
@patch("smoke_optimiser.cli.write_smoke_suite", new=MagicMock())
@patch("smoke_optimiser.cli.format_summary")
def test_cli_include_exclude(
    mock_format: MagicMock,
    mock_optimise: MagicMock,
    mock_run: MagicMock,
    profiled_suite: ProfilingData,
    tmp_path: Path,
) -> None:
    mock_run.return_value = ProfilingRun(data=profiled_suite, returncode=0)
    mock_optimise.return_value = MagicMock()
    mock_format.return_value = "Summary"

    with patch("pathlib.Path.cwd", return_value=tmp_path):
        result = runner.invoke(app, ["smoke", "--include", "test_a", "--include", "test_b", "--exclude", "test_c"])
    assert result.exit_code == EXIT_CODE_SUCCESS


def test_a_profile_with_a_current_schema_version_but_missing_fields_reports_a_corrupt_file_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A profile with the right schema_version but missing later-added fields must fail as a clean CLI error.

    ProfilingDataFile raises pydantic's ValidationError, which is neither an
    OSError nor a JSONDecodeError -- so before this was handled it escaped as
    a raw traceback. This must read as "failed to parse", not as a schema
    version mismatch, since the schema_version here is correct.
    """
    profile = tmp_path / "profile.json"
    profile.write_text(
        json.dumps({"schema_version": PROFILE_SCHEMA_VERSION, "meta": {}, "tests": {}, "total_branches": []})
    )

    with pytest.raises(typer.Exit):
        load_profile(profile)

    stderr = capsys.readouterr().err
    assert "Failed to parse profiling data" in stderr
    assert "schema version" not in stderr


def test_a_profile_from_an_older_schema_version_reports_a_distinct_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A stale schema_version must be reported as a version mismatch, not a generic parse failure.

    cli.py used to guess ('if this profile was recorded by an older version...') because it had
    no way to tell an old profile from a corrupt one. The recorded schema_version removes the guess,
    and the message must name both the found and expected versions.
    """
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({"schema_version": PROFILE_SCHEMA_VERSION - 1}))

    with pytest.raises(typer.Exit):
        load_profile(profile)

    stderr = capsys.readouterr().err
    assert str(PROFILE_SCHEMA_VERSION - 1) in stderr
    assert str(PROFILE_SCHEMA_VERSION) in stderr
    assert "reprofile" in stderr.lower() or "profiling phase" in stderr.lower()
    assert "Failed to parse profiling data" not in stderr


def test_a_profile_from_an_older_schema_version_suggests_the_command_that_wrote_it(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """When the stale profile recorded the command that produced it, the error hands it straight back.

    meta.command survives a schema bump untouched, so it can be read from the raw
    JSON even though the rest of the file fails validation. Printing it back turns
    "re-run the profiling phase" into a command the user can paste, rather than
    making them reconstruct the flags themselves.
    """
    profile = tmp_path / "profile.json"
    profile.write_text(
        json.dumps(
            {
                "schema_version": PROFILE_SCHEMA_VERSION - 1,
                "meta": {"command": "smoke-optimiser smoke --cov=smoke_optimiser"},
            }
        )
    )

    with pytest.raises(typer.Exit):
        load_profile(profile)

    stderr = capsys.readouterr().err
    assert "smoke-optimiser smoke --cov=smoke_optimiser" in stderr


def test_a_profile_recording_no_scope_reports_what_to_configure(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    raw_profile: RawProfileFactory,
) -> None:
    """A current-schema profile that cannot say what it measured needs its own message.

    Reprofiling alone may not fix it -- if no coverage target and no test path
    resolved inside the repository, the next profile records nothing again -- so
    the message names the settings to check rather than only the command to run.
    """
    profile = tmp_path / "profile.json"
    raw = raw_profile()
    raw["scope"] = {"coverage_roots": [], "test_roots": [], "test_file_patterns": []}
    profile.write_text(json.dumps(raw))

    with pytest.raises(typer.Exit):
        load_profile(profile)

    stderr = capsys.readouterr().err
    assert "no scope roots" in stderr
    assert "--cov" in stderr
    assert "testpaths" in stderr
    assert "Failed to parse profiling data" not in stderr


def _profile_recorded_with(workers: int) -> ProfilingData:
    """A minimal profile that claims to have been recorded with `workers` xdist workers."""
    meta = ProfilingMeta(
        timestamp=datetime(2026, 3, 2, 10, 30, 0, tzinfo=UTC),
        commit=None,
        python_version="3.12",
        coverage_version="7.0",
        command="smoke-optimiser",
        machine=MachineEnvironment(
            os=None,
            os_version=None,
            platform=None,
            architecture=None,
            cpu_model=None,
            cpu_cores_physical=None,
            cpu_cores_logical=None,
            ram_total_mb=None,
            ram_available_mb=None,
            hostname=None,
        ),
        xdist_workers=workers,
        iterations=1,
    )
    return ProfilingData(
        meta=meta,
        tests={},
        total_branches=frozenset(),
        measured_files=frozenset(),
        scope=ProfileScope(
            coverage_roots=frozenset({"smoke_optimiser"}),
            test_roots=frozenset({"tests"}),
            test_file_patterns=("test_*.py",),
        ),
        import_graph=ImportGraph(
            edges=frozenset(),
            unattributed_modules=frozenset(),
            resolution_errors=0,
            error_samples=(),
        ),
    )


def _default_config(*, allow_parallel_durations: bool) -> ResolvedConfig:
    return ResolvedConfig(
        mode=OperationMode.FULL,
        time_cap=15.0,
        target_cov=100.0,
        include_mandatory=[],
        exclude_mandatory=[],
        pytest_args="",
        output_json=Path(".smoke_suite.json"),
        allow_ordered=True,
        cov_source=".",
        cov_source_origin=CovSourceOrigin.CONFIGURED,
        iterations=1,
        allow_parallel_durations=allow_parallel_durations,
        profile_path=Path(".smoke_profiling_data.json"),
    )


def test_ranking_a_parallel_profile_is_refused() -> None:
    """Contended durations make a coverage-per-second ranking wrong but plausible.

    Nothing about the resulting smoke suite would look unusual, which is why this
    is an error rather than a warning the user can scroll past.
    """
    config = _default_config(allow_parallel_durations=False)

    with pytest.raises(typer.Exit) as exc_info:
        _reject_parallel_durations(config, _profile_recorded_with(8))

    assert exc_info.value.exit_code == EXIT_CODE_ERROR


def test_ranking_a_parallel_profile_is_allowed_with_the_override() -> None:
    config = _default_config(allow_parallel_durations=True)

    _reject_parallel_durations(config, _profile_recorded_with(8))


def test_a_serial_profile_is_ranked_without_complaint() -> None:
    config = _default_config(allow_parallel_durations=False)

    _reject_parallel_durations(config, _profile_recorded_with(1))


def _resolved_config_from_a_run(mock_run: MagicMock) -> ProfilingRunConfig:
    """Recover the config the CLI actually resolved, as handed to the profiler."""
    config = mock_run.call_args.args[0]
    assert isinstance(config, ProfilingRunConfig)
    return config


@patch("smoke_optimiser.cli.run_profiling")
@patch("smoke_optimiser.cli.optimise", new=MagicMock())
@patch("smoke_optimiser.cli.write_smoke_suite", new=MagicMock())
@patch("smoke_optimiser.cli.format_summary", new=MagicMock())
def test_a_boolean_set_in_pyproject_survives_a_run_that_does_not_mention_it(
    mock_run: MagicMock,
    profiled_suite: ProfilingData,
    tmp_path: Path,
) -> None:
    """End to end: the file setting reaches the profiler when no flag contradicts it."""
    (tmp_path / "pyproject.toml").write_text("[tool.smoke_optimiser]\nallow_ordered = true\n")
    mock_run.return_value = ProfilingRun(data=profiled_suite, returncode=0)

    with patch("pathlib.Path.cwd", return_value=tmp_path):
        result = runner.invoke(app, ["smoke"])

    assert result.exit_code == EXIT_CODE_SUCCESS
    assert _resolved_config_from_a_run(mock_run).allow_ordered is True


@patch("smoke_optimiser.cli.run_profiling")
@patch("smoke_optimiser.cli.optimise", new=MagicMock())
@patch("smoke_optimiser.cli.write_smoke_suite", new=MagicMock())
@patch("smoke_optimiser.cli.format_summary", new=MagicMock())
def test_the_negative_form_of_a_flag_turns_off_a_setting_the_file_turned_on(
    mock_run: MagicMock,
    profiled_suite: ProfilingData,
    tmp_path: Path,
) -> None:
    """The command line wins over pyproject.toml in both directions, not just one."""
    (tmp_path / "pyproject.toml").write_text("[tool.smoke_optimiser]\nallow_ordered = true\n")
    mock_run.return_value = ProfilingRun(data=profiled_suite, returncode=0)

    with patch("pathlib.Path.cwd", return_value=tmp_path):
        result = runner.invoke(app, ["smoke", "--no-allow-ordered"])

    assert result.exit_code == EXIT_CODE_SUCCESS
    assert _resolved_config_from_a_run(mock_run).allow_ordered is False
