from pathlib import Path
from unittest.mock import MagicMock, patch

from smoke_optimiser.config import OperationMode, ResolvedConfig
from smoke_optimiser.profiler.runner import check_prerequisites, run_profiling


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
    )
    with patch("shutil.which", return_value="/usr/bin/pytest"):
        check_prerequisites(config)


@patch("subprocess.run")
@patch("smoke_optimiser.profiler.runner.parse_coverage_json")
def test_run_profiling_basic(mock_parse: MagicMock, mock_run: MagicMock, tmp_path: Path) -> None:
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
    )

    mock_run.return_value = MagicMock(returncode=0, stdout="pytest-randomly")
    mock_parse.return_value = MagicMock()

    # Create dummy coverage file so check doesn't fail
    (tmp_path / ".smoke_optimiser_coverage.json").touch()

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
