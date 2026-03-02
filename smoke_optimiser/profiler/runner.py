import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import typer

from smoke_optimiser.config import ResolvedConfig
from smoke_optimiser.profiler.models import ProfilingData, ProfilingMeta
from smoke_optimiser.profiler.parser import parse_coverage_json

# Minimal inline pytest plugin to capture exact node IDs, durations, outcomes, and markers
PYTEST_HOOK_CODE = """
import json
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
        with open('.smoke_outcomes.json', 'w') as f:
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
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "--trace-config"],
            capture_output=True,
            text=True,
            check=False,
        )
        if "pytest-randomly" not in result.stdout:
            typer.secho(
                "Warning: pytest-randomly is not installed. Ordering-dependent tests produce unreliable smoke suites.",
                fg=typer.colors.YELLOW,
                err=True,
            )
            typer.echo("Use --allow-ordered to suppress this check.", err=True)
            sys.exit(1)


def _get_git_commit(project_root: Path) -> str | None:
    """Best-effort git commit retrieval."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
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

    coverage_json = project_root / ".smoke_optimiser_coverage.json"
    outcomes_json = project_root / ".smoke_outcomes.json"
    hook_file = project_root / "_smoke_hook.py"
    coveragerc = project_root / ".smoke_coveragerc"

    hook_file.write_text(PYTEST_HOOK_CODE)
    coveragerc.write_text(COVERAGERC_CONTENT)

    # 1. Run pytest with coverage and our hook
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
        args = config.pytest_args.split()
        pytest_cmd.extend(args)
        has_cov_arg = any(arg.startswith("--cov") for arg in args)

    if not has_cov_arg:
        # Check if the ResolvedConfig has a cov_source that came from CLI or heuristic
        # If it matches what we would discover now, and wasn't explicitly passed, warn.
        # But wait, config.cov_source ALWAYS has a value now.

        # To determine if heuristic was used, we'd need to know if it was None in CLI/file.
        # Let's simplify: if we are here and hasn't passed --cov in pytest_args,
        # we use config.cov_source.

        # The CLI 'main' knows if 'src' was None.
        # Let's just always print the warning if we are injecting --cov based on ResolvedConfig
        # unless it was explicitly provided via --src.
        # Actually, let's just use it.
        pytest_cmd.append(f"--cov={config.cov_source}")

    # Add project root to PYTHONPATH so pytest can find _smoke_hook and src code
    env = os.environ.copy()
    env["PYTHONPATH"] = str(project_root) + os.pathsep + env.get("PYTHONPATH", "")

    try:
        subprocess.run(pytest_cmd, cwd=project_root, check=False, env=env)

        # 2. Export coverage to JSON
        subprocess.run(
            [
                sys.executable,
                "-m",
                "coverage",
                "json",
                f"--rcfile={coveragerc}",
                "--show-contexts",
                "-o",
                str(coverage_json),
            ],
            cwd=project_root,
            check=False,
        )

        if not coverage_json.exists():
            typer.secho("Error: Coverage data was not generated.", fg=typer.colors.RED, err=True)
            sys.exit(1)

        # 3. Load outcomes
        test_durations = {}
        test_outcomes = {}
        test_markers = {}
        if outcomes_json.exists():
            with open(outcomes_json) as f:
                raw_outcomes = json.load(f)
                for nodeid, data in raw_outcomes.items():
                    test_durations[nodeid] = data["duration"]
                    test_outcomes[nodeid] = data["passed"]
                    test_markers[nodeid] = frozenset(data["markers"])

        # 4. Parse coverage JSON and merge
        data = parse_coverage_json(coverage_json, test_durations, test_outcomes, test_markers)

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
    finally:
        # Cleanup temporary files
        for f in [coverage_json, outcomes_json, hook_file, coveragerc]:
            if f.exists():
                f.unlink()
        # coverage.py also creates a .coverage file
        dot_coverage = project_root / ".coverage"
        if dot_coverage.exists():
            dot_coverage.unlink()
