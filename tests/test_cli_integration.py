from pathlib import Path
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from smoke_optimiser.cli import app
from smoke_optimiser.profiler.models import ProfilingData
from smoke_optimiser.profiler.persistence import load_profile
from smoke_optimiser.profiler.runner import ProfilingRun

runner = CliRunner()


@patch("smoke_optimiser.cli.run_profiling")
@patch("smoke_optimiser.cli.optimise")
@patch("smoke_optimiser.cli.write_smoke_suite", new=MagicMock())
@patch("smoke_optimiser.cli.format_summary")
def test_cli_full_run(
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

    assert result.exit_code == 0
    assert "Running profiling..." in result.stdout
    assert "Optimising smoke suite..." in result.stdout
    assert "Summary" in result.stdout
    mock_run.assert_called_once()
    mock_optimise.assert_called_once()


@patch("smoke_optimiser.cli.run_profiling")
@patch("smoke_optimiser.cli.optimise", new=MagicMock())
@patch("smoke_optimiser.cli.write_smoke_suite", new=MagicMock())
@patch("smoke_optimiser.cli.format_summary", new=MagicMock())
def test_an_ordinary_run_leaves_the_profile_behind_for_downwind(
    mock_run: MagicMock,
    profiled_suite: ProfilingData,
    tmp_path: Path,
) -> None:
    """The profile is an artefact of every run, not a resume point for --optimise-only.

    A user who runs ``smoke`` and then reaches for ``downwind`` must not be told
    to run the profiling phase they have just sat through.
    """
    with patch("pathlib.Path.cwd", return_value=tmp_path):
        mock_run.return_value = ProfilingRun(data=profiled_suite, returncode=0)
        result = runner.invoke(app, ["smoke"])

        assert result.exit_code == 0
        assert (tmp_path / ".smoke_profiling_data.json").exists()
        assert load_profile(tmp_path / ".smoke_profiling_data.json") == profiled_suite


@patch("smoke_optimiser.cli.run_profiling")
@patch("smoke_optimiser.cli.optimise", new=MagicMock())
@patch("smoke_optimiser.cli.write_smoke_suite", new=MagicMock())
@patch("smoke_optimiser.cli.format_summary", new=MagicMock())
def test_a_full_run_writes_the_profile_where_profile_path_asks_for_it(
    mock_run: MagicMock,
    profiled_suite: ProfilingData,
    tmp_path: Path,
) -> None:
    """The path both commands share is honoured on a full run, not just on --profile-only."""
    mock_run.return_value = ProfilingRun(data=profiled_suite, returncode=0)

    with patch("pathlib.Path.cwd", return_value=tmp_path):
        result = runner.invoke(app, ["smoke", "--profile-path", "custom-profile.json"])

    assert result.exit_code == 0
    assert (tmp_path / "custom-profile.json").exists()
    assert not (tmp_path / ".smoke_profiling_data.json").exists()


@patch("smoke_optimiser.cli.run_profiling")
def test_cli_profile_only(mock_run: MagicMock, profiled_suite: ProfilingData, tmp_path: Path) -> None:
    mock_run.return_value = ProfilingRun(data=profiled_suite, returncode=0)

    with patch("pathlib.Path.cwd", return_value=tmp_path):
        result = runner.invoke(app, ["smoke", "--profile-only"])

    assert result.exit_code == 0
    assert "Running profiling..." in result.stdout
    assert "Profiling data saved" in result.stdout
    assert (tmp_path / ".smoke_profiling_data.json").exists()
    # Nothing downstream of profiling ran, which is the whole point of the flag.
    assert "Optimising smoke suite..." not in result.stdout


def test_cli_optimise_only_no_data(tmp_path: Path) -> None:
    with patch("pathlib.Path.cwd", return_value=tmp_path):
        result = runner.invoke(app, ["smoke", "--optimise-only"])

    assert result.exit_code == 1
    assert "❌ Error: No profiling data found" in result.stderr
