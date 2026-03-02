from typer.testing import CliRunner

from smoke_optimiser.cli import app

runner = CliRunner()

EXIT_CODE_SUCCESS = 0
EXIT_CODE_ERROR = 1

TIME_CAP_VALUE = 45.0
TARGET_COV_VALUE = 80.0


def test_cli_help() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == EXIT_CODE_SUCCESS
    assert "smoke-optimiser" in result.stdout


def test_cli_defaults() -> None:
    result = runner.invoke(app, [])
    assert result.exit_code == EXIT_CODE_SUCCESS
    assert "Mode: full" in result.stdout
    assert "Time cap: 15.0s" in result.stdout


def test_cli_overrides() -> None:
    result = runner.invoke(app, ["--time-cap", str(TIME_CAP_VALUE), "--target-cov", str(TARGET_COV_VALUE)])
    assert result.exit_code == EXIT_CODE_SUCCESS
    assert f"Time cap: {TIME_CAP_VALUE}s" in result.stdout
    assert f"Target coverage: {TARGET_COV_VALUE}%" in result.stdout


def test_cli_profile_only() -> None:
    result = runner.invoke(app, ["--profile-only"])
    assert result.exit_code == EXIT_CODE_SUCCESS
    assert "Mode: profile-only" in result.stdout


def test_cli_optimise_only() -> None:
    result = runner.invoke(app, ["--optimise-only"])
    assert result.exit_code == EXIT_CODE_SUCCESS
    assert "Mode: optimise-only" in result.stdout


def test_cli_mutually_exclusive() -> None:
    result = runner.invoke(app, ["--profile-only", "--optimise-only"])
    assert result.exit_code == EXIT_CODE_ERROR
    assert "Error: --profile-only and --optimise-only are mutually exclusive." in result.stderr


def test_cli_include_exclude() -> None:
    # Multiple values for include/exclude
    result = runner.invoke(app, ["--include", "test_a", "--include", "test_b", "--exclude", "test_c"])
    assert result.exit_code == EXIT_CODE_SUCCESS
    # We don't print include/exclude yet, but this verifies they are accepted
