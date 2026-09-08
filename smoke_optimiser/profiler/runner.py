import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple, NewType, NoReturn

import pytest
import typer
from pydantic import ValidationError

from smoke_optimiser.config import ResolvedConfig
from smoke_optimiser.downwind.changes import GitStatusError, tracked_files
from smoke_optimiser.profiler.coverage_db import CoverageIngestError, build_profiling_data
from smoke_optimiser.profiler.import_tracer import ImportGraphIngestError, merge_graphs, read_graph
from smoke_optimiser.profiler.models import (
    ImportGraph,
    OutcomeRecordModel,
    OutcomesFileModel,
    ProfilingData,
    ProfilingMeta,
    ReadMap,
    SuiteRunResults,
)
from smoke_optimiser.profiler.read_tracer import (
    PYTHON_SUFFIX,
    ReadMapIngestError,
    merge_read_maps,
    read_read_map,
)
from smoke_optimiser.profiler.scope import ProfileScope

# An environment variable's value, named so the mapping the profiling subprocess
# runs under says what it carries rather than being an anonymous dict of strings.
EnvVarValue = NewType("EnvVarValue", str)


class OutcomesIngestError(RuntimeError):
    """Raised when the outcomes written by the profiling hook cannot be read."""


class IterationOutcomes(NamedTuple):
    """Outcomes from a single profiling iteration, merged across xdist workers."""

    outcomes: dict[str, OutcomeRecordModel]
    xdist_workers: int
    collection_errors: frozenset[str]
    scope: ProfileScope


# What each pytest exit code means for a profiling run. OK and TESTS_FAILED are absent
# because both leave a complete suite behind: a failing test is still a profiled test.
FATAL_EXIT_CODES: dict[pytest.ExitCode, str] = {
    pytest.ExitCode.INTERRUPTED: "pytest was interrupted before it finished the suite",
    pytest.ExitCode.INTERNAL_ERROR: "pytest hit an internal error",
    pytest.ExitCode.USAGE_ERROR: "pytest rejected its command line",
    pytest.ExitCode.NO_TESTS_COLLECTED: "pytest collected no tests",
}

USABLE_EXIT_CODES = frozenset({pytest.ExitCode.OK, pytest.ExitCode.TESTS_FAILED})


class HookArtefacts(NamedTuple):
    """The files the profiling hook writes, which the runner reads back.

    One group rather than three parameters: every one of them is written by the
    same hook, in the same temporary directory, and read back in the same step,
    so a caller that had one and not another would have nothing to do with it.

    Each is a base name. Under pytest-xdist every worker writes its own sibling
    of each, since sharing one file between workers loses all but the last.
    """

    outcomes_json: Path
    import_graph_json: Path
    read_map_json: Path


class IterationResult(NamedTuple):
    """Everything one profiling iteration left behind, including how pytest exited."""

    returncode: int
    outcomes: IterationOutcomes
    import_graph: ImportGraph
    read_map: ReadMap


# Minimal inline pytest plugin to capture exact node IDs, durations, outcomes, and markers
PYTEST_HOOK_CODE = """
import json
import os
import sys
from pathlib import Path

import pytest

from smoke_optimiser.profiler.import_tracer import ImportTracer, write_graph
from smoke_optimiser.profiler.read_tracer import ReadTracer, write_read_map
from smoke_optimiser.profiler.scope import resolve_scope

_TRACER = ImportTracer()
_READS = ReadTracer(Path(os.environ.get('SMOKE_PROJECT_ROOT', os.getcwd())))
_COLLECTION_ERRORS = []


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
    # Installed at the same moment, and for the mirror of the same reason: a file a
    # project module reads while being imported is a real dependency with no test to
    # attribute it to, which is exactly what the unattributed read set is for.
    _READS.install()

def pytest_collectreport(report):
    if report.failed:
        # A file that will not import contributes no tests and no coverage, yet the
        # rest of the run proceeds normally under --continue-on-collection-errors and
        # exits 1 -- indistinguishable from ordinary test failures. Recording it here
        # is what lets the runner tell the two apart.
        _COLLECTION_ERRORS.append(report.nodeid or 'the test session root')


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    # The whole protocol, not just the call phase, so a fixture that reads a fixture
    # file attributes to the test that used it -- matching how coverage records its
    # own contexts across setup, call and teardown.
    _READS.set_active_test(item.nodeid)
    yield
    _READS.set_active_test(None)


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
    _READS.uninstall()
    root = Path(os.environ.get('SMOKE_PROJECT_ROOT', str(config.rootpath)))
    graph_file = os.environ.get('SMOKE_IMPORT_GRAPH_JSON')
    if graph_file:
        write_graph(_TRACER.snapshot(root, sys.modules), Path(_worker_path(graph_file)))

    read_map_file = os.environ.get('SMOKE_READ_MAP_JSON')
    if read_map_file:
        write_read_map(_READS.snapshot(), Path(_worker_path(read_map_file)))

    # Read here rather than from the command line the runner built: the project's
    # own addopts and testpaths are applied by pytest inside this process and are
    # invisible outside it, so this is the only place the scope actually measured
    # can be observed.
    scope = resolve_scope(
        cov_sources=getattr(config.option, 'cov_source', None) or [],
        args=config.args,
        test_file_patterns=config.getini('python_files'),
        invocation_dir=Path(str(config.invocation_params.dir)),
        project_root=root,
    )

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
            'collection_errors': _COLLECTION_ERRORS,
            'scope': {
                'coverage_roots': sorted(scope.coverage_roots),
                'test_roots': sorted(scope.test_roots),
                'test_file_patterns': list(scope.test_file_patterns),
            },
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
    collection_errors: set[str] = set()
    scopes: list[ProfileScope] = []

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
        collection_errors.update(written.collection_errors)
        scopes.append(written.scope.to_profile_scope())
        if written.worker_count:
            workers = max(workers, written.worker_count)
        path.unlink()

    return IterationOutcomes(
        outcomes=merged,
        xdist_workers=workers,
        collection_errors=frozenset(collection_errors),
        scope=_merge_scopes(scopes),
    )


def _merge_scopes(scopes: Sequence[ProfileScope]) -> ProfileScope:
    """Union the scopes every process of one run reported.

    A serial run reports one. Under pytest-xdist the controller and every worker
    parse the same command line, so they agree -- unioning them rather than
    picking one means a worker that somehow saw more is not silently discarded,
    since a scope that is too narrow is the direction that under-selects.
    """
    return ProfileScope(
        coverage_roots=frozenset().union(*(scope.coverage_roots for scope in scopes)) if scopes else frozenset(),
        test_roots=frozenset().union(*(scope.test_roots for scope in scopes)) if scopes else frozenset(),
        test_file_patterns=tuple(sorted({pattern for scope in scopes for pattern in scope.test_file_patterns})),
    )


def _warn_about_an_unbounded_test_scope(scope: ProfileScope) -> None:
    """Say what a project loses by leaving pytest pointed at the repository root.

    The profile still works, so this is not fatal -- but the guarantee it carries
    is narrower than the user has any way of knowing, so the steps that widen it
    are spelled out rather than hinted at.
    """
    if not scope.collects_from_whole_repository:
        return
    typer.secho(
        "\u26a0\ufe0f Warning: pytest has no configured test paths, so the whole repository is in scope and "
        "only files matching its test-file patterns can be tracked. New test support modules (fixtures, "
        "factories, helpers) will not be noticed when the profile goes stale, and any test-named file "
        "elsewhere in the tree -- a vendored package's suite, an example, a manual script -- makes every "
        "downwind run fall back to the full suite.",
        fg=typer.colors.YELLOW,
        err=True,
    )
    typer.secho(
        "\U0001f4a1 To fix:\n"
        "  1. Create a tests/ directory at the repository root and move every test file into it.\n"
        '  2. Add testpaths = ["tests"] under [tool.pytest.ini_options] in pyproject.toml.\n'
        "  3. Re-run smoke-optimiser to regenerate the profile.",
        fg=typer.colors.YELLOW,
        err=True,
    )


def _collection_error_message(collection_errors: frozenset[str]) -> str:
    """Explain why a run that pytest was content to finish cannot be profiled."""
    listed = "\n".join(f"  {node_id}" for node_id in sorted(collection_errors))
    counted = "1 file" if len(collection_errors) == 1 else f"{len(collection_errors)} files"
    pronoun = "it" if len(collection_errors) == 1 else "them"
    return (
        f"pytest could not collect {counted}:\n"
        f"{listed}\n"
        f"No test in {pronoun} ran, so the smoke suite would be selected from a suite that is quietly "
        "smaller than the real one. Fix the collection errors and profile again."
    )


def _fatal_exit_message(returncode: int) -> str:
    """Explain a pytest exit code that leaves the suite only partly profiled."""
    try:
        exit_code = pytest.ExitCode(returncode)
    except ValueError:
        return (
            f"pytest exited with code {returncode}, which is not one it defines -- it was most likely "
            "killed or it crashed. The suite it profiled is incomplete."
        )
    # .get, not [], so an exit code a later pytest adds is reported rather than raising
    # a KeyError out of the very code whose job is to explain what went wrong.
    meaning = FATAL_EXIT_CODES.get(exit_code, f"pytest exited with {exit_code.name}")
    return f"{meaning} (exit code {int(exit_code)}). The suite it profiled is incomplete."


def _read_iteration_graph(import_graph_json: Path) -> ImportGraph:
    """Read and merge every import graph one profiling iteration produced."""
    paths = sorted(import_graph_json.parent.glob(f"{import_graph_json.stem}*{import_graph_json.suffix}"))
    graphs = [read_graph(path) for path in paths]
    for path in paths:
        path.unlink()
    return merge_graphs(graphs)


def _read_iteration_read_map(read_map_json: Path) -> ReadMap:
    """Read and merge every file read map one profiling iteration produced."""
    paths = sorted(read_map_json.parent.glob(f"{read_map_json.stem}*{read_map_json.suffix}"))
    maps = [read_read_map(path) for path in paths]
    for path in paths:
        path.unlink()
    return merge_read_maps(maps)


def _present_files(project_root: Path) -> frozenset[str]:
    """The tracked non-Python files that exist as the run starts.

    The read map's denominator, so that a file nothing opened can be told apart
    from a file that was not there to be opened. Best effort, as the commit is:
    profiling does not otherwise need git, and an empty denominator makes every
    data file look new, which over-selects rather than under-selects.
    """
    try:
        tracked = tracked_files(project_root)
    except GitStatusError as exc:
        typer.secho(
            f"\u26a0\ufe0f Warning: could not list the tracked files ({exc.detail.strip()}), so the profile "
            "records no denominator for the file read map. Downwind selection will treat every data file as "
            "newly added, which widens its answers rather than narrowing them.",
            fg=typer.colors.YELLOW,
            err=True,
        )
        return frozenset()
    return frozenset(path for path in tracked if not path.endswith(PYTHON_SUFFIX))


def _profiling_env(
    temp_dir: Path,
    project_root: Path,
    coverage_db: Path,
    artefacts: HookArtefacts,
) -> dict[str, EnvVarValue]:
    """Build the environment the profiled pytest subprocess runs under."""
    env = {name: EnvVarValue(value) for name, value in os.environ.items()}

    current_pythonpath = env.get("PYTHONPATH", EnvVarValue(""))
    # Add temp_dir to PYTHONPATH so pytest can load _smoke_hook
    parts = [str(temp_dir), str(project_root)]
    if current_pythonpath:
        parts.append(current_pythonpath)
    env["PYTHONPATH"] = EnvVarValue(os.pathsep.join(parts))

    env["SMOKE_OUTCOMES_JSON"] = EnvVarValue(str(artefacts.outcomes_json))
    env["SMOKE_IMPORT_GRAPH_JSON"] = EnvVarValue(str(artefacts.import_graph_json))
    env["SMOKE_READ_MAP_JSON"] = EnvVarValue(str(artefacts.read_map_json))
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


def _fail(message: str) -> NoReturn:
    """Report a fatal profiling problem and stop, rather than emit a partial profile."""
    typer.secho(f"\u274c Error: {message}", fg=typer.colors.RED, err=True)
    sys.exit(1)


def _build_pytest_command(config: ResolvedConfig, coveragerc: Path) -> list[str]:
    """Build the pytest command line one profiling iteration runs."""
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

    return pytest_cmd


def _run_iteration(
    pytest_cmd: list[str],
    project_root: Path,
    env: dict[str, EnvVarValue],
    artefacts: HookArtefacts,
) -> IterationResult:
    """Run the suite once and collect everything that iteration left behind."""
    # the command is built from sys.executable and user-provided args in a local CLI tool
    run = subprocess.run(pytest_cmd, cwd=project_root, check=False, env=env)  # noqa: S603

    # The files are read whatever the exit code, both to leave the temp directory clean
    # for the next iteration and because the outcomes file is what carries the
    # collection errors -- which need reporting however pytest chose to exit.
    try:
        outcomes = _read_iteration_outcomes(artefacts.outcomes_json)
    except OutcomesIngestError as exc:
        _fail(str(exc))
    try:
        import_graph = _read_iteration_graph(artefacts.import_graph_json)
    except ImportGraphIngestError as exc:
        _fail(str(exc))
    try:
        read_map = _read_iteration_read_map(artefacts.read_map_json)
    except ReadMapIngestError as exc:
        _fail(str(exc))

    return IterationResult(
        returncode=run.returncode,
        outcomes=outcomes,
        import_graph=import_graph,
        read_map=read_map,
    )


def run_profiling(config: ResolvedConfig, project_root: Path) -> ProfilingData:
    """Run the test suite under coverage instrumentation and collect results."""
    check_prerequisites(config)

    with tempfile.TemporaryDirectory() as temp_dir_str:
        temp_dir = Path(temp_dir_str)
        artefacts = HookArtefacts(
            outcomes_json=temp_dir / "outcomes.json",
            import_graph_json=temp_dir / "import_graph.json",
            read_map_json=temp_dir / "read_map.json",
        )
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
        read_maps: list[ReadMap] = []
        scopes: list[ProfileScope] = []
        # Before the first iteration, so the denominator names the tree the suite was
        # actually profiled against rather than whatever it became while it ran.
        present_files = _present_files(project_root)

        env = _profiling_env(temp_dir, project_root, coverage_db, artefacts)
        pytest_cmd = _build_pytest_command(config, coveragerc)

        for i in range(config.iterations):
            if config.iterations > 1:
                typer.secho(f"  \U0001f504 Iteration {i + 1}/{config.iterations}...", fg=typer.colors.CYAN)

            result = _run_iteration(pytest_cmd, project_root, env, artefacts)

            if result.outcomes.collection_errors:
                # Fatal however many iterations have already run: the same files fail to
                # collect every time, so no amount of accumulated coverage makes up for
                # the tests that were never collected at all.
                _fail(_collection_error_message(result.outcomes.collection_errors))

            if result.returncode not in USABLE_EXIT_CODES:
                reason = _fatal_exit_message(result.returncode)
                # Every iteration that does not complete leaves the loop here, so i is
                # exactly the number of iterations that did complete.
                if i == 0:
                    _fail(reason)
                # Coverage accumulates into one database across iterations, so the work
                # the earlier ones did is intact and still worth a profile. This
                # iteration's durations are not: tests it never reached would be
                # averaged over fewer samples than the rest, so its outcomes and import
                # graph are dropped whole.
                plural = "" if i == 1 else "s"
                typer.secho(
                    f"\u26a0\ufe0f Warning: iteration {i + 1}/{config.iterations} did not finish -- {reason} "
                    f"Profiling continues from the {i} iteration{plural} that did.",
                    fg=typer.colors.YELLOW,
                    err=True,
                )
                break

            xdist_workers = max(xdist_workers, result.outcomes.xdist_workers)
            graphs.append(result.import_graph)
            read_maps.append(result.read_map)
            scopes.append(result.outcomes.scope)

            for nodeid, record in result.outcomes.outcomes.items():
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
            scope=_merge_scopes(scopes),
            read_map=merge_read_maps(read_maps),
            present_files=present_files,
        )
        _warn_about_an_unbounded_test_scope(results.scope)
        try:
            data = build_profiling_data(coverage_db, project_root, results, config_file=coveragerc)
        except CoverageIngestError as exc:
            _fail(str(exc))

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
            scope=data.scope,
            unattributable_branches=data.unattributable_branches,
            reads=data.reads,
            present_files=data.present_files,
        )
