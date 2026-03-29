from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from smoke_optimiser.cli import app

runner = CliRunner()


@patch("smoke_optimiser.cli.run_profiling")
@patch("smoke_optimiser.cli.optimise")
@patch("smoke_optimiser.cli.write_smoke_suite")
@patch("smoke_optimiser.cli.format_summary")
def test_cli_full_run(
    mock_format: MagicMock,
    mock_write: MagicMock,
    mock_optimise: MagicMock,
    mock_run: MagicMock,
    tmp_path: Path,
) -> None:
    mock_run.return_value = MagicMock(tests={}, total_branches=frozenset(), meta=MagicMock())
    mock_optimise.return_value = MagicMock()
    mock_format.return_value = "Summary"

    with patch("pathlib.Path.cwd", return_value=tmp_path):
        result = runner.invoke(app, [])

    assert result.exit_code == 0
    assert "Running profiling..." in result.stdout
    assert "Optimising smoke suite..." in result.stdout
    assert "Summary" in result.stdout
    mock_run.assert_called_once()
    mock_optimise.assert_called_once()


@patch("smoke_optimiser.cli.run_profiling")
def test_cli_profile_only(mock_run: MagicMock, tmp_path: Path) -> None:
    # We need a proper meta object that can be dumped to dict and used in ProfilingMetaModel
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

    assert result.exit_code == 0
    assert "Running profiling..." in result.stdout
    assert "Profiling data saved" in result.stdout
    assert (tmp_path / ".smoke_profiling_data.json").exists()


def test_cli_optimise_only_no_data(tmp_path: Path) -> None:
    with patch("pathlib.Path.cwd", return_value=tmp_path):
        result = runner.invoke(app, ["--optimise-only"])

    assert result.exit_code == 1
    assert "❌ Error: No profiling data found" in result.stderr
