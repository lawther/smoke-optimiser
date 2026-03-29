from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from smoke_optimiser.cli import app

runner = CliRunner()

EXIT_CODE_SUCCESS = 0
EXIT_CODE_ERROR = 1

TIME_CAP_VALUE = 45.0
TARGET_COV_VALUE = 80.0


@patch("smoke_optimiser.cli.run_profiling")
@patch("smoke_optimiser.cli.optimise")
@patch("smoke_optimiser.cli.write_smoke_suite")
@patch("smoke_optimiser.cli.format_summary")
def test_cli_help(
    mock_format: MagicMock,
    mock_write: MagicMock,
    mock_optimise: MagicMock,
    mock_run: MagicMock,
) -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == EXIT_CODE_SUCCESS
    assert "smoke-optimiser" in result.stdout


@patch("smoke_optimiser.cli.run_profiling")
@patch("smoke_optimiser.cli.optimise")
@patch("smoke_optimiser.cli.write_smoke_suite")
@patch("smoke_optimiser.cli.format_summary")
def test_cli_defaults(
    mock_format: MagicMock,
    mock_write: MagicMock,
    mock_optimise: MagicMock,
    mock_run: MagicMock,
) -> None:
    mock_run.return_value = MagicMock(tests={}, total_branches=frozenset(), meta=MagicMock())
    mock_optimise.return_value = MagicMock()
    mock_format.return_value = "Summary"

    result = runner.invoke(app, [])
    assert result.exit_code == EXIT_CODE_SUCCESS


@patch("smoke_optimiser.cli.run_profiling")
@patch("smoke_optimiser.cli.optimise")
@patch("smoke_optimiser.cli.write_smoke_suite")
@patch("smoke_optimiser.cli.format_summary")
def test_cli_overrides(
    mock_format: MagicMock,
    mock_write: MagicMock,
    mock_optimise: MagicMock,
    mock_run: MagicMock,
) -> None:
    mock_run.return_value = MagicMock(tests={}, total_branches=frozenset(), meta=MagicMock())
    mock_optimise.return_value = MagicMock()
    mock_format.return_value = "Summary"

    result = runner.invoke(app, ["--time-cap", str(TIME_CAP_VALUE), "--target-cov", str(TARGET_COV_VALUE)])
    assert result.exit_code == EXIT_CODE_SUCCESS


@patch("smoke_optimiser.cli.run_profiling")
@patch("smoke_optimiser.cli.optimise")
@patch("smoke_optimiser.cli.write_smoke_suite")
@patch("smoke_optimiser.cli.format_summary")
def test_cli_profile_only(
    mock_format: MagicMock,
    mock_write: MagicMock,
    mock_optimise: MagicMock,
    mock_run: MagicMock,
    tmp_path: Path,
) -> None:
    meta = MagicMock()
    meta.timestamp = datetime(2026, 3, 2, 10, 30, 0, tzinfo=UTC)
    meta.commit = "abcdef"
    meta.python_version = "3.12"
    meta.coverage_version = "7.0"
    meta.command = "smoke-optimiser"
    meta.machine.os = "Linux"
    meta.machine.os_version = "6.5"
    meta.machine.platform = "Ubuntu"
    meta.machine.architecture = "x86_64"
    meta.machine.cpu_model = "AMD"
    meta.machine.cpu_cores_physical = 16
    meta.machine.cpu_cores_logical = 32
    meta.machine.ram_total_mb = 65536
    meta.machine.ram_available_mb = 58200
    meta.machine.hostname = "ci-04"

    mock_run.return_value = MagicMock(
        tests={},
        total_branches=frozenset(),
        meta=meta,
    )

    with patch("pathlib.Path.cwd", return_value=tmp_path):
        result = runner.invoke(app, ["--profile-only"])
    assert result.exit_code == EXIT_CODE_SUCCESS


def test_cli_mutually_exclusive() -> None:
    result = runner.invoke(app, ["--profile-only", "--optimise-only"])
    assert result.exit_code == EXIT_CODE_ERROR
    assert "❌ Error: --profile-only and --optimise-only are mutually exclusive." in result.stderr


@patch("smoke_optimiser.cli.run_profiling")
@patch("smoke_optimiser.cli.optimise")
@patch("smoke_optimiser.cli.write_smoke_suite")
@patch("smoke_optimiser.cli.format_summary")
def test_cli_include_exclude(
    mock_format: MagicMock,
    mock_write: MagicMock,
    mock_optimise: MagicMock,
    mock_run: MagicMock,
) -> None:
    mock_run.return_value = MagicMock(tests={}, total_branches=frozenset(), meta=MagicMock())
    mock_optimise.return_value = MagicMock()
    mock_format.return_value = "Summary"

    result = runner.invoke(app, ["--include", "test_a", "--include", "test_b", "--exclude", "test_c"])
    assert result.exit_code == EXIT_CODE_SUCCESS
