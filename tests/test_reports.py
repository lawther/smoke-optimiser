import re
from datetime import UTC, datetime
from pathlib import Path

from typer.testing import CliRunner

from smoke_optimiser.cli import app
from smoke_optimiser.config import OperationMode, ResolvedConfig
from smoke_optimiser.environment import MachineEnvironment
from smoke_optimiser.optimiser.models import SmokeResult
from smoke_optimiser.profiler.models import ProfilingMeta
from smoke_optimiser.reports.repro import build_repro_command
from smoke_optimiser.reports.smoke_suite import read_smoke_suite, write_smoke_suite
from smoke_optimiser.reports.summary import format_summary

TOTAL_BRANCHES = 1000
TOTAL_TESTS = 100
PASSED_TESTS = 100
FULL_RUNTIME = 300.0
SMOKE_RUNTIME = 30.0
SMOKE_COVERAGE = 100.0


def test_build_repro_command() -> None:
    config = ResolvedConfig(
        mode=OperationMode.FULL,
        time_cap=15.0,
        target_cov=100.0,
        include_mandatory=["@pytest.mark.smoke"],
        exclude_mandatory=["@pytest.mark.slow"],
        pytest_args="--timeout=30",
        output_json=Path(".smoke_suite.json"),
        allow_ordered=False,
        cov_source="src",
        iterations=1,
        allow_parallel_durations=False,
    )
    cmd = build_repro_command(config)
    assert "smoke-optimiser" in cmd
    assert "--time-cap=15.0" in cmd
    assert "--target-cov=100.0" in cmd
    assert "--include=@pytest.mark.smoke" in cmd
    assert "--exclude=@pytest.mark.slow" in cmd
    assert "--pytest-args=--timeout=30" in cmd
    assert "--output-json=.smoke_suite.json" in cmd
    # allow_ordered is off in this config, and the flag has no negative form, so the
    # reproducing command must simply leave it out.
    assert "allow-ordered" not in cmd
    assert "--src=src" in cmd


def test_build_repro_command_empty_lists() -> None:
    config = ResolvedConfig(
        mode=OperationMode.FULL,
        time_cap=15.0,
        target_cov=100.0,
        include_mandatory=[],
        exclude_mandatory=[],
        pytest_args="",
        output_json=Path(".smoke_suite.json"),
        allow_ordered=False,
        cov_source="src",
        iterations=1,
        allow_parallel_durations=False,
    )
    cmd = build_repro_command(config)
    assert "--include=''" in cmd
    assert "--exclude=''" in cmd
    assert "--src=src" in cmd


def test_repro_command_completeness_against_help() -> None:
    """Introspect --help output and ensure all non-flag arguments are in repro.

    Flags are booleans defaulting to False.
    """
    runner = CliRunner()
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0

    # 1. Find all options (lines starting with --)
    # The regex finds "--name"
    all_options = re.findall(r"--([a-z-]+)", result.stdout)

    # 2. Exclude standard typer boilerplate and the exempt mode flags
    # We ALSO exclude smoke-file-path because it's a plugin concern
    exclusions = {
        "install-completion",
        "show-completion",
        "help",
        "profile-only",
        "optimise-only",
        "smoke-file-path",
    }
    required_options = [opt for opt in all_options if opt not in exclusions]

    # Generate a repro command with default config
    config = ResolvedConfig(
        mode=OperationMode.FULL,
        time_cap=15.0,
        target_cov=100.0,
        include_mandatory=[],
        exclude_mandatory=[],
        pytest_args="",
        output_json=Path(".smoke_suite.json"),
        allow_ordered=True,
        cov_source=".",
        iterations=1,
        allow_parallel_durations=True,
    )
    cmd = build_repro_command(config)

    # Boolean flags carry no value and exist only in their positive form, so the config
    # above turns every one of them on and the command must then name each of them.
    boolean_flags = {"allow-ordered", "allow-parallel-durations"}

    for opt in required_options:
        if opt in boolean_flags:
            assert f"--{opt}" in cmd, f"Option --{opt} missing from canonical repro command"
        else:
            # Check if "--opt=" is in the command
            assert f"--{opt}=" in cmd, f"Option --{opt} missing from canonical repro command"


def test_smoke_suite_roundtrip(tmp_path: Path) -> None:
    meta = ProfilingMeta(
        timestamp=datetime(2026, 3, 2, 10, 30, 0, tzinfo=UTC),
        commit="abcdef",
        python_version="3.12",
        coverage_version="7.0",
        command="smoke-optimiser",
        machine=MachineEnvironment(
            os="Linux",
            os_version="6.5",
            platform="Ubuntu",
            architecture="x86_64",
            cpu_model="AMD",
            cpu_cores_physical=16,
            cpu_cores_logical=32,
            ram_total_mb=65536,
            ram_available_mb=58200,
            hostname="ci-04",
        ),
        xdist_workers=1,
    )
    result = SmokeResult(
        selected_tests=[],
        total_tests_profiled=TOTAL_TESTS,
        tests_passed=PASSED_TESTS,
        tests_failed=0,
        total_branches=TOTAL_BRANCHES,
        full_suite_branches_covered=TOTAL_BRANCHES,
        smoke_branches_covered=TOTAL_BRANCHES,
        smoke_coverage_pct=SMOKE_COVERAGE,
        full_suite_runtime_s=FULL_RUNTIME,
        smoke_suite_runtime_s=SMOKE_RUNTIME,
        coverage_equivalents=[],
    )
    config = ResolvedConfig(
        mode=OperationMode.FULL,
        time_cap=15.0,
        target_cov=100.0,
        include_mandatory=[],
        exclude_mandatory=[],
        pytest_args="",
        output_json=Path(".smoke_suite.json"),
        allow_ordered=False,
        cov_source=".",
        iterations=1,
        allow_parallel_durations=False,
    )
    output_file = tmp_path / ".smoke_suite.json"
    write_smoke_suite(result, config, meta, output_file)

    loaded = read_smoke_suite(output_file)
    assert loaded.summary.total_branches == TOTAL_BRANCHES
    assert loaded.machine.os == "Linux"


def test_format_summary() -> None:
    meta = ProfilingMeta(
        timestamp=datetime.now(UTC),
        commit=None,
        python_version="3.12",
        coverage_version="7.0",
        command="smoke-optimiser",
        machine=MachineEnvironment(
            os="Linux",
            os_version="6.5",
            platform="Ubuntu",
            architecture="x86_64",
            cpu_model="AMD",
            cpu_cores_physical=16,
            cpu_cores_logical=32,
            ram_total_mb=65536,
            ram_available_mb=58200,
            hostname="ci-04",
        ),
        xdist_workers=1,
    )
    result = SmokeResult(
        selected_tests=[],
        total_tests_profiled=10,
        tests_passed=9,
        tests_failed=1,
        total_branches=100,
        full_suite_branches_covered=90,
        smoke_branches_covered=80,
        smoke_coverage_pct=80.0,
        full_suite_runtime_s=100.0,
        smoke_suite_runtime_s=10.0,
        coverage_equivalents=[],
    )
    config = ResolvedConfig(
        mode=OperationMode.FULL,
        time_cap=15.0,
        target_cov=100.0,
        include_mandatory=[],
        exclude_mandatory=[],
        pytest_args="",
        output_json=Path(".smoke_suite.json"),
        allow_ordered=False,
        cov_source=".",
        iterations=1,
        allow_parallel_durations=False,
    )
    summary = format_summary(result, config, meta)
    assert "smoke-optimiser results" in summary
    assert "80.0%" in summary
    assert "1 failing test was excluded" in summary
    assert "Coverage:     90 / 100 branches (90.0%)" in summary
    # Nothing is unattributable here, so no ceiling should be claimed.
    assert "Ceiling:" not in summary


def test_format_summary_reports_the_attainable_ceiling() -> None:
    """Branches that only run at import time make 100% unreachable, and we say so."""
    meta = ProfilingMeta(
        timestamp=datetime.now(UTC),
        commit=None,
        python_version="3.12",
        coverage_version="7.0",
        command="smoke-optimiser",
        machine=MachineEnvironment(
            os="Linux",
            os_version="6.5",
            platform="Ubuntu",
            architecture="x86_64",
            cpu_model="AMD",
            cpu_cores_physical=16,
            cpu_cores_logical=32,
            ram_total_mb=65536,
            ram_available_mb=58200,
            hostname="ci-04",
        ),
        xdist_workers=1,
    )
    result = SmokeResult(
        selected_tests=[],
        total_tests_profiled=10,
        tests_passed=10,
        tests_failed=0,
        total_branches=100,
        full_suite_branches_covered=90,
        smoke_branches_covered=80,
        smoke_coverage_pct=80.0,
        full_suite_runtime_s=100.0,
        smoke_suite_runtime_s=10.0,
        coverage_equivalents=[],
        unattributable_branches=4,
    )
    config = ResolvedConfig(
        mode=OperationMode.FULL,
        time_cap=15.0,
        target_cov=100.0,
        include_mandatory=[],
        exclude_mandatory=[],
        pytest_args="",
        output_json=Path(".smoke_suite.json"),
        allow_ordered=False,
        cov_source=".",
        iterations=1,
        allow_parallel_durations=False,
    )

    summary = format_summary(result, config, meta)

    assert "Ceiling:      96.0% max attainable" in summary
    assert "4 branches only execute outside any test" in summary
