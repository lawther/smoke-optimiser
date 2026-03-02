import json
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from smoke_optimiser.config import ResolvedConfig
from smoke_optimiser.profiler.models import ProfilingData, ProfilingMeta
from smoke_optimiser.profiler.parser import parse_coverage_json

# Small inline pytest plugin to capture exact node IDs and outcomes
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
            print(
                "Warning: pytest-randomly is not installed. Ordering-dependent tests produce unreliable smoke suites.",
                file=sys.stderr,
            )
            print("Use --allow-ordered to suppress this check.", file=sys.stderr)
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
    hook_file = project_root / ".smoke_hook.py"

    hook_file.write_text(PYTEST_HOOK_CODE)

    # 1. Run pytest with coverage and our hook
    pytest_cmd = [
        sys.executable,
        "-m",
        "pytest",
        "-p",
        ".smoke_hook",
        "--cov",
        "--cov-branch",
        "--cov-context=test",
    ]
    if config.pytest_args:
        pytest_cmd.extend(config.pytest_args.split())

    try:
        subprocess.run(pytest_cmd, cwd=project_root, check=False)

        # 2. Export coverage to JSON
        subprocess.run(
            [
                sys.executable,
                "-m",
                "coverage",
                "json",
                "--show-contexts",
                "-o",
                str(coverage_json),
            ],
            cwd=project_root,
            check=False,
        )

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
        for f in [coverage_json, outcomes_json, hook_file]:
            if f.exists():
                f.unlink()
        # coverage.py also creates a .coverage file
        dot_coverage = project_root / ".coverage"
        if dot_coverage.exists():
            dot_coverage.unlink()
