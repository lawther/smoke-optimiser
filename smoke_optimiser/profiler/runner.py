import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

import typer

from smoke_optimiser.config import ResolvedConfig
from smoke_optimiser.profiler.models import ProfilingData, ProfilingMeta
from smoke_optimiser.profiler.parser import parse_coverage_json

# Minimal inline pytest plugin to capture exact node IDs, durations, outcomes, and markers
PYTEST_HOOK_CODE = """
import json
import os
import pytest

def pytest_configure(config):
    config._smoke_outcomes = {}

@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if report.when == 'call':
        item.config._smoke_outcomes[item.nodeid] = {
            'passed': report.passed,
            'duration': report.duration,
            'markers': [m.name for m in item.iter_markers()]
        }

def pytest_unconfigure(config):
    if hasattr(config, '_smoke_outcomes'):
        # Unique name per worker if needed, but here we just need one
        outcomes_file = os.environ.get('SMOKE_OUTCOMES_JSON', '.smoke_outcomes.json')
        try:
            os.unlink(outcomes_file)
        except OSError:
            pass
        with open(outcomes_file, 'w') as f:
            json.dump(config._smoke_outcomes, f)
"""

COVERAGERC_CONTENT = """
[run]
branch = True

[json]
show_contexts = True
"""


def check_prerequisites(config: ResolvedConfig) -> None:
    """Verify that all necessary tools are available."""
    if shutil.which("pytest") is None:
        raise RuntimeError("pytest not found in PATH")

    if not config.allow_ordered:
        # Check if pytest-randomly is installed
        result = subprocess.run(  # noqa: S603 - calling the current python interpreter
            [sys.executable, "-m", "pytest", "--trace-config"],
            capture_output=True,
            text=True,
            check=False,
        )
        if "pytest-randomly" not in result.stdout:
            typer.secho(
                "⚠ Warning: pytest-randomly is not installed. "
                "Ordering-dependent tests produce unreliable smoke suites.",
                fg=typer.colors.YELLOW,
                err=True,
            )
            typer.secho("💡 Use --allow-ordered to suppress this check.", fg=typer.colors.YELLOW, err=True)
            sys.exit(1)


def _get_git_commit(project_root: Path) -> str | None:
    """Best-effort git commit retrieval."""
    git_path = shutil.which("git") or "/usr/bin/git"
    try:
        result = subprocess.run(  # noqa: S603
            [git_path, "rev-parse", "HEAD"],
            cwd=project_root,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def run_profiling(config: ResolvedConfig, project_root: Path) -> ProfilingData:
    """Run the test suite under coverage instrumentation and collect results."""
    check_prerequisites(config)

    with tempfile.TemporaryDirectory() as temp_dir_str:
        temp_dir = Path(temp_dir_str)
        coverage_json = temp_dir / "coverage.json"
        outcomes_json = temp_dir / "outcomes.json"
        hook_file = temp_dir / "_smoke_hook.py"
        coveragerc = temp_dir / ".coveragerc"
        coverage_db = temp_dir / ".coverage"

        hook_file.write_text(PYTEST_HOOK_CODE)
        coveragerc.write_text(COVERAGERC_CONTENT)

        # 1. Run pytest iterations
        # We aggregate durations across runs
        all_durations: dict[str, list[float]] = defaultdict(list)
        final_outcomes: dict[str, bool] = {}
        final_markers: dict[str, frozenset[str]] = {}

        env = os.environ.copy()
        current_pythonpath = env.get("PYTHONPATH", "")
        # Add temp_dir to PYTHONPATH so pytest can load _smoke_hook
        parts = [str(temp_dir), str(project_root)]
        if current_pythonpath:
            parts.append(current_pythonpath)
        env["PYTHONPATH"] = os.pathsep.join(parts)

        env["SMOKE_OUTCOMES_JSON"] = str(outcomes_json)
        env["COVERAGE_FILE"] = str(coverage_db)

        for i in range(config.iterations):
            if config.iterations > 1:
                typer.secho(f"  🔄 Iteration {i + 1}/{config.iterations}...", fg=typer.colors.CYAN)

            pytest_cmd = [
                sys.executable,
                "-m",
                "pytest",
                "-p",
                "_smoke_hook",
                f"--cov-config={coveragerc}",
                "--cov-branch",
                "--cov-context=test",
            ]

            has_cov_arg = False
            if config.pytest_args:
                args = shlex.split(config.pytest_args)
                pytest_cmd.extend(args)
                has_cov_arg = any(arg.startswith("--cov") for arg in args)

            if not has_cov_arg:
                pytest_cmd.append(f"--cov={config.cov_source}")

            # the command is built from sys.executable and user-provided args in a local CLI tool
            subprocess.run(pytest_cmd, cwd=project_root, check=False, env=env)  # noqa: S603

            # Load outcomes from this run
            if outcomes_json.exists():
                with open(outcomes_json) as f:
                    raw_outcomes = json.load(f)
                    for nodeid, data in raw_outcomes.items():
                        all_durations[nodeid].append(data["duration"])
                        # Use the last run's outcome/markers (should be consistent)
                        final_outcomes[nodeid] = data["passed"]
                        final_markers[nodeid] = frozenset(data["markers"])
                outcomes_json.unlink()

        # 2. Export coverage to JSON (from the last run)
        # We capture_output=True to prevent coverage.py from printing the "Wrote JSON report" message
        subprocess.run(  # noqa: S603 - calling current python for coverage tool
            [
                sys.executable,
                "-m",
                "coverage",
                "json",
                f"--data-file={coverage_db}",
                f"--rcfile={coveragerc}",
                "--show-contexts",
                "-o",
                str(coverage_json),
            ],
            cwd=project_root,
            capture_output=True,
            text=True,
            check=False,
        )

        if not coverage_json.exists():
            typer.secho("❌ Error: Coverage data was not generated.", fg=typer.colors.RED, err=True)
            sys.exit(1)


        # 3. Average the durations
        avg_durations = {nodeid: sum(durations) / len(durations) for nodeid, durations in all_durations.items()}

        # 4. Parse coverage JSON and merge
        data = parse_coverage_json(coverage_json, avg_durations, final_outcomes, final_markers)

        # Fill in the missing metadata
        final_meta = ProfilingMeta(
            timestamp=datetime.now(UTC),
            commit=_get_git_commit(project_root),
            python_version=sys.version,
            coverage_version=data.meta.coverage_version,
            command=" ".join(sys.argv),
            machine=data.meta.machine,
        )

        return ProfilingData(
            meta=final_meta,
            tests=data.tests,
            total_branches=data.total_branches,
        )
