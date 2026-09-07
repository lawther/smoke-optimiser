import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple, NewType

import typer
from pydantic import ValidationError

from smoke_optimiser.config import ResolvedConfig
from smoke_optimiser.profiler.coverage_db import CoverageIngestError, build_profiling_data
from smoke_optimiser.profiler.import_tracer import ImportGraphIngestError, merge_graphs, read_graph
from smoke_optimiser.profiler.models import (
    ImportGraph,
    OutcomeRecordModel,
    OutcomesFileModel,
    ProfilingData,
    ProfilingMeta,
    SuiteRunResults,
)

# An environment variable's value, named so the mapping the profiling subprocess
# runs under says what it carries rather than being an anonymous dict of strings.
EnvVarValue = NewType("EnvVarValue", str)


class OutcomesIngestError(RuntimeError):
    """Raised when the outcomes written by the profiling hook cannot be read."""


class IterationOutcomes(NamedTuple):
    """Outcomes from a single profiling iteration, merged across xdist workers."""

    outcomes: dict[str, OutcomeRecordModel]
    xdist_workers: int


# Minimal inline pytest plugin to capture exact node IDs, durations, outcomes, and markers
PYTEST_HOOK_CODE = """
import json
import os
import sys
from pathlib import Path

import pytest

from smoke_optimiser.profiler.import_tracer import ImportTracer, write_graph

_TRACER = ImportTracer()


def _worker_path(path):
    '''Give each xdist worker its own file, as sibling workers overwrite a shared one.'''
    worker = os.environ.get('PYTEST_XDIST_WORKER')
    if not worker:
        return path
    base, ext = os.path.splitext(path)
    return base + '.' + worker + ext


def pytest_configure(config):
    config._smoke_outcomes = {}
    # Project modules are imported during collection, which is still ahead of us, so
    # this is early enough to see them. Imports made before now -- pytest's own start-up
    # and this plugin's -- are missed, and none of those are project files.
    _TRACER.install()

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
    elif report.when in ('setup', 'teardown') and report.failed and item.nodeid not in item.config._smoke_outcomes:
        # A test whose fixture errors during setup never reaches 'call', so it would
        # otherwise be absent from outcomes while its setup-phase coverage still lands
        # in the coverage database under this test's context, tripping _verify_contexts.
        # A teardown error after a passing call is deliberately NOT recorded here, so
        # the call phase's passed=True is preserved.
        item.config._smoke_outcomes[item.nodeid] = {
            'passed': False,
            'duration': report.duration,
            'markers': [m.name for m in item.iter_markers()]
        }

def pytest_unconfigure(config):
    _TRACER.uninstall()
    graph_file = os.environ.get('SMOKE_IMPORT_GRAPH_JSON')
    if graph_file:
        root = Path(os.environ.get('SMOKE_PROJECT_ROOT', str(config.rootpath)))
        write_graph(_TRACER.snapshot(root, sys.modules), Path(_worker_path(graph_file)))

    if hasattr(config, '_smoke_outcomes'):
        # Under pytest-xdist every worker runs this hook. They must not share one
        # file: each would unlink and rewrite it, and the runner would read back
        # whichever worker happened to finish last, silently losing the rest of
        # the suite's durations, outcomes and markers. The worker id comes from
        # this process's own environment rather than from the command line,
        # because a project can switch xdist on through pytest.ini addopts
        # without any flag ever reaching smoke-optimiser.
        outcomes_file = _worker_path(os.environ.get('SMOKE_OUTCOMES_JSON', '.smoke_outcomes.json'))
        worker = os.environ.get('PYTEST_XDIST_WORKER')
        worker_count = os.environ.get('PYTEST_XDIST_WORKER_COUNT')
        try:
            os.unlink(outcomes_file)
        except OSError:
            pass
        payload = {
            'worker': worker,
            'worker_count': int(worker_count) if worker_count else None,
            'outcomes': config._smoke_outcomes,
        }
        with open(outcomes_file, 'w') as f:
            json.dump(payload, f)
"""

COVERAGERC_CONTENT = """
[run]
branch = True
"""


def check_prerequisites(config: ResolvedConfig) -> None:
    """Verify that all necessary tools are available."""
    if shutil.which("pytest") is None:
        msg = "pytest not found in PATH"
        raise RuntimeError(msg)

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
                "⚠️ Warning: pytest-randomly is not installed. "
                "Ordering-dependent tests produce unreliable smoke suites.",
                fg=typer.colors.YELLOW,
                err=True,
            )
            typer.secho("💡 Use --allow-ordered to suppress this check.", fg=typer.colors.YELLOW, err=True)
            sys.exit(1)


def _get_git_commit(project_root: Path) -> str | None:
    """Best-effort git commit retrieval."""
    git_path = shutil.which("git")
    if not git_path:
        return None

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


def _read_iteration_outcomes(outcomes_json: Path) -> IterationOutcomes:
    """Read and merge every outcomes file one profiling iteration produced.

    A serial run writes a single file. Under pytest-xdist each worker writes its
    own, plus an empty one from the controller process, so the caller must union
    them rather than reading one path.
    """
    merged: dict[str, OutcomeRecordModel] = {}
    workers = 1

    for path in sorted(outcomes_json.parent.glob(f"{outcomes_json.stem}*{outcomes_json.suffix}")):
        try:
            with path.open("rb") as f:
                written = OutcomesFileModel.model_validate_json(f.read())
        except (OSError, ValidationError) as exc:
            msg = (
                f"could not read the test outcomes written by the profiling hook ({path}): {exc}. "
                "Every test's duration, outcome and markers come from this file, so a partial read "
                "would silently profile only part of the suite."
            )
            raise OutcomesIngestError(msg) from exc

        merged.update(written.outcomes)
        if written.worker_count:
            workers = max(workers, written.worker_count)
        path.unlink()

    return IterationOutcomes(outcomes=merged, xdist_workers=workers)


def _read_iteration_graph(import_graph_json: Path) -> ImportGraph:
    """Read and merge every import graph one profiling iteration produced."""
    paths = sorted(import_graph_json.parent.glob(f"{import_graph_json.stem}*{import_graph_json.suffix}"))
    graphs = [read_graph(path) for path in paths]
    for path in paths:
        path.unlink()
    return merge_graphs(graphs)


def _profiling_env(
    temp_dir: Path,
    project_root: Path,
    outcomes_json: Path,
    coverage_db: Path,
    import_graph_json: Path,
) -> dict[str, EnvVarValue]:
    """Build the environment the profiled pytest subprocess runs under."""
    env = {name: EnvVarValue(value) for name, value in os.environ.items()}

    current_pythonpath = env.get("PYTHONPATH", EnvVarValue(""))
    # Add temp_dir to PYTHONPATH so pytest can load _smoke_hook
    parts = [str(temp_dir), str(project_root)]
    if current_pythonpath:
        parts.append(current_pythonpath)
    env["PYTHONPATH"] = EnvVarValue(os.pathsep.join(parts))

    env["SMOKE_OUTCOMES_JSON"] = EnvVarValue(str(outcomes_json))
    env["SMOKE_IMPORT_GRAPH_JSON"] = EnvVarValue(str(import_graph_json))
    env["COVERAGE_FILE"] = EnvVarValue(str(coverage_db))
    # The graph's paths must be relative to the same root as the coverage data, which
    # pytest's own rootdir is not obliged to match.
    env["SMOKE_PROJECT_ROOT"] = EnvVarValue(str(project_root.resolve()))

    # smoke-optimiser may itself be running inside someone else's xdist worker, whose
    # worker variables would otherwise be inherited by this serial child and recorded
    # as the profiled suite's own parallelism. A child that really does use xdist sets
    # these itself in the workers it spawns.
    env.pop("PYTEST_XDIST_WORKER", None)
    env.pop("PYTEST_XDIST_WORKER_COUNT", None)

    return env


def run_profiling(config: ResolvedConfig, project_root: Path) -> ProfilingData:
    """Run the test suite under coverage instrumentation and collect results."""
    check_prerequisites(config)

    with tempfile.TemporaryDirectory() as temp_dir_str:
        temp_dir = Path(temp_dir_str)
        outcomes_json = temp_dir / "outcomes.json"
        import_graph_json = temp_dir / "import_graph.json"
        hook_file = temp_dir / "_smoke_hook.py"
        coveragerc = temp_dir / ".coveragerc"
        coverage_db = temp_dir / ".coverage"

        hook_file.write_text(PYTEST_HOOK_CODE)
        coveragerc.write_text(COVERAGERC_CONTENT)

        # Run pytest, aggregating durations across iterations
        all_durations: dict[str, list[float]] = defaultdict(list)
        final_outcomes: dict[str, bool] = {}
        final_markers: dict[str, frozenset[str]] = {}
        xdist_workers = 1
        graphs: list[ImportGraph] = []

        env = _profiling_env(temp_dir, project_root, outcomes_json, coverage_db, import_graph_json)

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

            has_cov_source = False
            if config.pytest_args:
                args = shlex.split(config.pytest_args)
                pytest_cmd.extend(args)
                # Only --cov itself sets what is measured; --cov-report and friends do not.
                has_cov_source = any(arg == "--cov" or arg.startswith("--cov=") for arg in args)

            if not has_cov_source:
                pytest_cmd.append(f"--cov={config.cov_source}")

            # the command is built from sys.executable and user-provided args in a local CLI tool
            subprocess.run(pytest_cmd, cwd=project_root, check=False, env=env)  # noqa: S603

            # Load outcomes from this run
            try:
                iteration = _read_iteration_outcomes(outcomes_json)
            except OutcomesIngestError as exc:
                typer.secho(f"\u274c Error: {exc}", fg=typer.colors.RED, err=True)
                sys.exit(1)
            xdist_workers = max(xdist_workers, iteration.xdist_workers)
            try:
                graphs.append(_read_iteration_graph(import_graph_json))
            except ImportGraphIngestError as exc:
                typer.secho(f"\u274c Error: {exc}", fg=typer.colors.RED, err=True)
                sys.exit(1)

            for nodeid, record in iteration.outcomes.items():
                all_durations[nodeid].append(record.duration)
                # Use the last run's outcome/markers (should be consistent)
                final_outcomes[nodeid] = record.passed
                final_markers[nodeid] = frozenset(record.markers)

        avg_durations = {nodeid: sum(durations) / len(durations) for nodeid, durations in all_durations.items()}

        # Read per-test coverage straight out of coverage.py's SQLite database
        results = SuiteRunResults(
            durations=avg_durations,
            outcomes=final_outcomes,
            markers=final_markers,
            xdist_workers=xdist_workers,
            import_graph=merge_graphs(graphs),
        )
        try:
            data = build_profiling_data(coverage_db, project_root, results, config_file=coveragerc)
        except CoverageIngestError as exc:
            typer.secho(f"\u274c Error: {exc}", fg=typer.colors.RED, err=True)
            sys.exit(1)

        # Fill in the missing metadata
        final_meta = ProfilingMeta(
            timestamp=datetime.now(UTC),
            commit=_get_git_commit(project_root),
            python_version=sys.version,
            coverage_version=data.meta.coverage_version,
            command=" ".join(sys.argv),
            machine=data.meta.machine,
            xdist_workers=data.meta.xdist_workers,
        )

        return ProfilingData(
            meta=final_meta,
            tests=data.tests,
            total_branches=data.total_branches,
            measured_files=data.measured_files,
            import_graph=data.import_graph,
            unattributable_branches=data.unattributable_branches,
        )
