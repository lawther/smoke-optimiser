import importlib.util
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

from smoke_optimiser.config import CovSourceOrigin, ProfilingRunConfig
from smoke_optimiser.downwind.changes import GitStatusError, working_tree_files
from smoke_optimiser.paths import ProjectPaths
from smoke_optimiser.profiler.coverage_db import CoverageIngestError, build_profiling_data
from smoke_optimiser.profiler.import_tracer import ImportGraphIngestError, merge_graphs, read_graph
from smoke_optimiser.profiler.models import (
    ImportGraph,
    OutcomeRecordModel,
    OutcomesFileModel,
    ProfileAnchor,
    ProfilingData,
    ProfilingMeta,
    ReadMap,
    SuiteRunResults,
)
from smoke_optimiser.profiler.read_tracer import (
    ReadMapIngestError,
    merge_read_maps,
    read_read_map,
)
from smoke_optimiser.profiler.scope import WHOLE_REPOSITORY, ProfileScope

# An environment variable's value, named so the mapping the profiling subprocess
# runs under says what it carries rather than being an anonymous dict of strings.
EnvVarValue = NewType("EnvVarValue", str)


class OutcomesIngestError(RuntimeError):
    """Raised when the outcomes written by the profiling hook cannot be read."""


class ProfilingUnavailableError(RuntimeError):
    """Raised when profiling cannot be attempted at all, so no suite ran.

    Kept apart from :class:`ProfilingIncompleteError` because the two leave the
    caller in different places: nothing has been run here, so a caller that
    needed the suite run -- the downwind fallback -- still has to run it,
    uninstrumented.
    """


class ProfilingIncompleteError(RuntimeError):
    """Raised when a suite ran but did not yield a profile that can be trusted.

    Raised rather than exited so that a caller with more to do than stop can do
    it. ``smoke`` catches this and exits 1, exactly as it always did; the
    downwind fallback catches it, leaves the previous profile untouched, and
    still has a pytest exit code to honour.

    ``returncode`` is how the profiled pytest exited, where that is known. It is
    None for the failures that happen before or after any single run can be
    blamed -- reading back the coverage database, say -- and a caller with a
    verdict to reach should treat that as "no verdict from pytest" rather than
    as success.
    """

    def __init__(self, message: str, returncode: int | None = None) -> None:
        super().__init__(message)
        self.returncode = returncode


class IterationOutcomes(NamedTuple):
    """Outcomes from a single profiling iteration, merged across xdist workers."""

    outcomes: dict[str, OutcomeRecordModel]
    xdist_workers: int
    collection_errors: frozenset[str]
    scope: ProfileScope
    node_id_prefix: str


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


class ArtefactFileCounts(NamedTuple):
    """How many files one iteration left behind for each artefact the hook writes.

    Every process that runs the hook -- a serial run's single one, or the xdist
    controller and each of its workers -- writes one file of every kind in the same
    unconfigure, so the three counts agree unless a process died part-way through.
    """

    outcomes: int
    import_graph: int
    read_map: int


class IterationResult(NamedTuple):
    """Everything one profiling iteration left behind, including how pytest exited."""

    returncode: int
    outcomes: IterationOutcomes
    import_graph: ImportGraph
    read_map: ReadMap
    artefact_files: ArtefactFileCounts


class SuiteObservations(NamedTuple):
    """What the iterations observed, before the denominator only the caller knows.

    Everything in :class:`SuiteRunResults` except ``present_files``, which is the
    state of the tree BEFORE the first iteration and so cannot be gathered by the
    loop that runs them.
    """

    durations: dict[str, float]
    outcomes: dict[str, bool]
    markers: dict[str, frozenset[str]]
    xdist_workers: int
    iterations: int
    import_graph: ImportGraph
    scope: ProfileScope
    read_map: ReadMap
    node_id_prefix: str
    returncode: int
    """How the last pytest invocation exited. Kept because a caller may be running
    the suite for its own sake and not only to profile it, and would otherwise have
    to rerun it to learn whether the tests passed."""


class ProfilingRun(NamedTuple):
    """A completed profiling run: the profile, and how the suite that made it exited.

    Two values rather than one because the run answers two questions at once for
    the downwind fallback -- what the map is now, and whether these tests pass --
    and only the second can gate a commit.
    """

    data: ProfilingData
    returncode: int


# Minimal inline pytest plugin to capture exact node IDs, durations, outcomes, and markers
PYTEST_HOOK_CODE = """
import json
import os
import sys
from pathlib import Path

import pytest

from smoke_optimiser.paths import offset_from
from smoke_optimiser.profiler.import_tracer import ImportTracer, write_graph
from smoke_optimiser.profiler.read_tracer import ReadTracer, write_read_map
from smoke_optimiser.profiler.scope import resolve_scope

_TRACER = ImportTracer()
_READS = ReadTracer(Path(os.environ.get('SMOKE_REPO_ROOT', os.getcwd())))
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
    root = Path(os.environ.get('SMOKE_REPO_ROOT', str(config.rootpath)))
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
    cov_plugin = config.pluginmanager.get_plugin('_cov')
    cov_controller = getattr(cov_plugin, 'cov_controller', None) if cov_plugin else None
    cov_config = getattr(getattr(cov_controller, 'cov', None), 'config', None)
    # Read from the Coverage object actually doing the measuring, not assumed: a
    # project that has turned this on is not subject to the package-walk
    # restriction coverage otherwise applies to every one of its source dirs.
    include_namespace_packages = bool(cov_config.include_namespace_packages) if cov_config is not None else False
    scope = resolve_scope(
        cov_sources=getattr(config.option, 'cov_source', None) or [],
        args=config.args,
        test_file_patterns=config.getini('python_files'),
        invocation_dir=Path(str(config.invocation_params.dir)),
        repo_root=root,
        include_namespace_packages=include_namespace_packages,
    )

    # ROOTDIR, not invocation_params.dir. Node ids are rootdir-relative while
    # pytest's positional arguments are cwd-relative, so resolve_scope above is
    # right to use the other one -- and the two coincide only until they do not.
    node_id_prefix = offset_from(root, Path(str(config.rootpath)))

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
                'include_namespace_packages': scope.include_namespace_packages,
            },
            'node_id_prefix': node_id_prefix,
        }
        with open(outcomes_file, 'w') as f:
            json.dump(payload, f)
"""

COVERAGERC_CONTENT = """
[run]
branch = True
"""


HEURISTIC_COV_SOURCES: dict[CovSourceOrigin, str] = {
    CovSourceOrigin.COVERAGE_CONFIG: "from [tool.coverage.run] in pyproject.toml",
    CovSourceOrigin.SRC_LAYOUT: "because this project has a src/ directory",
    CovSourceOrigin.PROJECT_NAME: "from the project name in pyproject.toml",
}
"""How each guessed coverage source was arrived at, in words.

Every one of these is announced. What a profile instruments decides what it can
ever know, so a value the user did not choose has to be visible at the moment it
is acted on -- a run that quietly widened the coverage root from a package to the
whole repository is how a profile acquires files coverage can never measure, and
with them an expiry no amount of regenerating clears. CONFIGURED is absent
because the user already knows; UNDISCOVERED is absent because it is refused
rather than announced.
"""


def _invocation() -> str:
    """This run's own command line, ready to be pasted back with something added."""
    return " ".join([Path(sys.argv[0]).name, *(shlex.quote(arg) for arg in sys.argv[1:])])


def _no_coverage_source_message() -> str:
    """Refuse to instrument, and hand back the three commands that would.

    Refused rather than defaulted to the whole repository, because that default is
    silently destructive: every .py file in the tree goes into the profile's scope,
    including the ones coverage.py never walks -- anything outside an importable
    package, such as a directory of hook scripts -- and a file that can never be
    measured is a file the profile is permanently missing, which expires it on
    every run for ever.

    Instrumenting everything is still a perfectly reasonable thing to want, which
    is why the last line offers exactly that: refusing to GUESS it is not the same
    as refusing to do it.
    """
    invocation = _invocation()
    return (
        "no coverage source is configured and none could be discovered, so there is nothing to "
        "instrument.\n"
        "   Instrumenting the whole repository is not a safe default: it puts every .py file in the "
        "tree into the profile's scope, including ones coverage.py never measures, and a file the "
        "profile can never know expires it on every run.\n"
        "   Set it once, in pyproject.toml:\n"
        "     [tool.smoke_optimiser]\n"
        '     cov_source = "your_package"\n'
        "   Or just for this run:\n"
        f"     {invocation} --src=your_package\n"
        "   Or, to instrument the whole repository deliberately:\n"
        f"     {invocation} --src=."
    )


def _announce_cov_source(config: ProfilingRunConfig) -> None:
    """Say what is being instrumented whenever the user did not say it themselves."""
    explanation = HEURISTIC_COV_SOURCES.get(config.cov_source_origin)
    if explanation is None:
        return
    typer.secho(
        f"⚠️ Warning: no coverage source given, so instrumenting --src={config.cov_source}, {explanation}.",
        fg=typer.colors.YELLOW,
        err=True,
    )


def supplies_cov_source(pytest_args: str) -> bool:
    """Do these pytest arguments already say what coverage measures?

    Shared with :func:`_build_pytest_command`, which appends ``--cov`` only when
    they do not: a second spelling of this question that disagreed would either
    instrument twice or refuse a run that had a perfectly good coverage source
    all along. Only --cov itself sets what is measured; --cov-report and friends
    do not.
    """
    return any(arg == "--cov" or arg.startswith("--cov=") for arg in shlex.split(pytest_args))


def check_prerequisites(config: ProfilingRunConfig) -> None:
    """Verify that all necessary tools are available."""
    if shutil.which("pytest") is None:
        msg = "pytest not found in PATH"
        raise ProfilingUnavailableError(msg)

    # A --cov in the user's own arguments settles the question, so neither the
    # refusal nor the announcement below has anything to say about a value that
    # is never going to reach the command line.
    if supplies_cov_source(config.pytest_args):
        return

    if config.cov_source_origin is CovSourceOrigin.UNDISCOVERED:
        raise ProfilingUnavailableError(_no_coverage_source_message())

    _announce_cov_source(config)

    # Checked directly against the installed distributions rather than by running
    # `pytest --trace-config` in a subprocess: with nothing else restricting it,
    # that invocation collects and runs the entire profiled suite serially, just
    # to grep its banner for the plugin's name -- exactly the slow, single-core
    # detour this check exists to avoid inflicting on the profiling run itself.
    #
    # A warning and not a refusal: what it costs is a smoke suite ranked from
    # order-dependent timings, which is a worse suite rather than a wrong map,
    # and the same profile is what downwind selects from. Refusing to record it
    # would mean declining to run the developer's tests over a missing plugin.
    if not config.allow_ordered and importlib.util.find_spec("pytest_randomly") is None:
        typer.secho(
            "⚠️ Warning: pytest-randomly is not installed. Ordering-dependent tests produce unreliable smoke suites.",
            fg=typer.colors.YELLOW,
            err=True,
        )
        typer.secho("💡 Use --allow-ordered to suppress this check.", fg=typer.colors.YELLOW, err=True)


def _get_git_commit(repo_root: Path) -> str | None:
    """Best-effort git commit retrieval."""
    git_path = shutil.which("git")
    if not git_path:
        return None

    try:
        result = subprocess.run(  # noqa: S603
            [git_path, "rev-parse", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def _artefact_files(base: Path) -> list[Path]:
    """Every file one iteration's processes wrote for a single artefact.

    A serial run writes the base name alone. Under pytest-xdist the controller and
    each worker write a sibling of it, since sharing one file between them keeps
    only the last writer's contribution.
    """
    return sorted(base.parent.glob(f"{base.stem}*{base.suffix}"))


def _read_iteration_outcomes(paths: Sequence[Path]) -> IterationOutcomes:
    """Read and merge every outcomes file one profiling iteration produced.

    A serial run writes a single file. Under pytest-xdist each worker writes its
    own, plus an empty one from the controller process, so the caller must union
    them rather than reading one path.
    """
    merged: dict[str, OutcomeRecordModel] = {}
    workers = 1
    collection_errors: set[str] = set()
    scopes: list[ProfileScope] = []
    prefixes: list[str] = []

    for path in paths:
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
        prefixes.append(written.node_id_prefix)
        if written.worker_count:
            workers = max(workers, written.worker_count)
        path.unlink()

    return IterationOutcomes(
        outcomes=merged,
        xdist_workers=workers,
        collection_errors=frozenset(collection_errors),
        scope=_merge_scopes(scopes),
        node_id_prefix=_agreed_node_id_prefix(prefixes),
    )


def _agreed_node_id_prefix(prefixes: Sequence[str]) -> str:
    """The one rootdir prefix every process reported, or refuse the run.

    Unlike the scope this is not unioned. The prefix decides which path space
    the file a node id names is read in, so two of them would put one set of
    maps in two spaces at once -- the exact failure this prefix exists to
    prevent, arriving from the other direction. Every process parses the same
    rootdir, so disagreement means something is wrong that guessing would hide.
    """
    distinct = set(prefixes)
    if len(distinct) > 1:
        listed = ", ".join(sorted(distinct))
        msg = (
            f"the profiling hook reported {len(distinct)} different pytest rootdirs ({listed}). "
            "Node ids are rootdir-relative, so one profile cannot be keyed by two of them without "
            "silently selecting nothing for half the tree."
        )
        raise OutcomesIngestError(msg)
    return next(iter(distinct), WHOLE_REPOSITORY)


def _merge_scopes(scopes: Sequence[ProfileScope]) -> ProfileScope:
    """Union the scopes every process of one run reported.

    A serial run reports one. Under pytest-xdist the controller and every worker
    parse the same command line, so they agree -- unioning them rather than
    picking one means a worker that somehow saw more is not silently discarded,
    since a scope that is too narrow is the direction that under-selects.

    ``include_namespace_packages`` is the one field this is not really a union
    of: every process shares the same coverage configuration, so they should
    all report the same value. Taking the most permissive one if they somehow
    disagreed keeps the same bias as the rest of this merge.
    """
    return ProfileScope(
        coverage_roots=frozenset().union(*(scope.coverage_roots for scope in scopes)) if scopes else frozenset(),
        test_roots=frozenset().union(*(scope.test_roots for scope in scopes)) if scopes else frozenset(),
        test_file_patterns=tuple(sorted({pattern for scope in scopes for pattern in scope.test_file_patterns})),
        include_namespace_packages=any(scope.include_namespace_packages for scope in scopes),
    )


def _warn_about_an_unbounded_test_scope(scope: ProfileScope) -> None:
    """Say what a project loses by leaving pytest pointed at its whole tree.

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
        "elsewhere in the tree (a vendored package's suite, an example, a manual script) makes every "
        "downwind run fall back to the full suite.",
        fg=typer.colors.YELLOW,
        err=True,
    )
    typer.secho(
        "\U0001f4a1 To fix:\n"
        "  1. Create a tests/ directory beside your pyproject.toml and move every test file into it.\n"
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


def _missing_artefact_message(counts: ArtefactFileCounts) -> str | None:
    """Explain artefacts that a cleanly-exiting pytest still failed to leave behind.

    Absence has to be reported, because alone among the failures here it reads as
    valid data rather than as an error: a graph with no edges says nothing in the
    project imports anything, and a read map with no reads says no test opened any
    file. Every file then looks like a file nothing depends on, so a change to it
    would select no tests -- the silent under-selection the profile exists to
    prevent.

    The three counts are checked against each other because each process writes one
    of every artefact in the same unconfigure, so under xdist one dead worker shows
    up as a set that is one short rather than as nothing at all. Which artefact is
    short depends on where the process died -- the graph is written before the
    outcomes -- so the expected count is the largest of the three, not the outcomes.
    """
    if not any(counts):
        return (
            "pytest exited cleanly but left no profiling artefacts at all, so its shutdown hook never "
            "ran and nothing about the suite was recorded. An empty import graph does not read as "
            "'unknown', it reads as 'nothing in this project imports anything', which would make a "
            "change to any file select no tests. Profile again."
        )

    expected = max(counts)
    shortfalls = [
        f"{label} ({count} of {expected})"
        for label, count in (
            ("test outcomes", counts.outcomes),
            ("import graph", counts.import_graph),
            ("file read map", counts.read_map),
        )
        if count != expected
    ]
    if not shortfalls:
        return None

    listed = ", ".join(shortfalls)
    return (
        f"pytest exited cleanly, but of the {expected} processes it ran, not all of them left every "
        f"artefact behind: {listed}. A process killed part-way through writing them -- a timeout, an "
        "OOM kill, an xdist worker that died -- leaves a graph missing the edges it never recorded, "
        "and missing edges read as 'nothing imports those modules' rather than as 'unknown', so a "
        "change to them would select no tests. Profile again."
    )


def _read_iteration_graph(paths: Sequence[Path]) -> ImportGraph:
    """Read and merge every import graph one profiling iteration produced."""
    graphs = [read_graph(path) for path in paths]
    for path in paths:
        path.unlink()
    return merge_graphs(graphs)


def _read_iteration_read_map(paths: Sequence[Path]) -> ReadMap:
    """Read and merge every file read map one profiling iteration produced."""
    maps = [read_read_map(path) for path in paths]
    for path in paths:
        path.unlink()
    return merge_read_maps(maps)


def _present_files(repo_root: Path) -> frozenset[str]:
    """Every non-ignored file in the working tree as the run starts.

    The profile's denominator, so that a file nothing touched can be told apart
    from a file that was not there to be touched. Both halves of the profile
    need it and both need the same one: the read map, so an unopened data file
    is answerable, and the Python maps, so a module the run never loaded is
    inert by measurement rather than merely unheard of.

    Python files are in it for that second reader. It records what EXISTED,
    which is a question about the working tree rather than about any recorder,
    so what coverage or the import tracer would have said about a file is no
    reason to leave it out.

    Best effort, as the commit is: profiling does not otherwise need git, and
    an empty denominator makes every file look new, which over-selects rather
    than under-selects.
    """
    try:
        return working_tree_files(repo_root)
    except GitStatusError as exc:
        typer.secho(
            f"\u26a0\ufe0f Warning: could not list the working tree ({exc.detail.strip()}), so the profile "
            "records no denominator. Downwind selection will treat every file it has no other record of as "
            "newly added, which widens its answers rather than narrowing them.",
            fg=typer.colors.YELLOW,
            err=True,
        )
        return frozenset()


def _profiling_env(
    temp_dir: Path,
    paths: ProjectPaths,
    coverage_db: Path,
    artefacts: HookArtefacts,
) -> dict[str, EnvVarValue]:
    """Build the environment the profiled pytest subprocess runs under."""
    env = {name: EnvVarValue(value) for name, value in os.environ.items()}

    current_pythonpath = env.get("PYTHONPATH", EnvVarValue(""))
    # Add temp_dir to PYTHONPATH so pytest can load _smoke_hook
    parts = [str(temp_dir), str(paths.invocation_dir)]
    if current_pythonpath:
        parts.append(current_pythonpath)
    env["PYTHONPATH"] = EnvVarValue(os.pathsep.join(parts))

    env["SMOKE_OUTCOMES_JSON"] = EnvVarValue(str(artefacts.outcomes_json))
    env["SMOKE_IMPORT_GRAPH_JSON"] = EnvVarValue(str(artefacts.import_graph_json))
    env["SMOKE_READ_MAP_JSON"] = EnvVarValue(str(artefacts.read_map_json))
    env["COVERAGE_FILE"] = EnvVarValue(str(coverage_db))
    # The graph's paths must be relative to the same root as the coverage data, which
    # pytest's own rootdir is not obliged to match -- and in a monorepo does not.
    env["SMOKE_REPO_ROOT"] = EnvVarValue(str(paths.repo_root.resolve()))

    # smoke-optimiser may itself be running inside someone else's xdist worker, whose
    # worker variables would otherwise be inherited by this serial child and recorded
    # as the profiled suite's own parallelism. A child that really does use xdist sets
    # these itself in the workers it spawns.
    env.pop("PYTEST_XDIST_WORKER", None)
    env.pop("PYTEST_XDIST_WORKER_COUNT", None)

    return env


def _fail(message: str, returncode: int | None = None) -> NoReturn:
    """Refuse to emit a profile, rather than emit a partial one.

    Raises rather than exits: the caller decides what a failed profiling run
    costs. For ``smoke`` it is the whole command; for the downwind fallback it
    means the previous profile stands and the suite that just ran still has an
    exit code to report -- which is why ``returncode`` is carried wherever the
    caller could know it.
    """
    raise ProfilingIncompleteError(message, returncode)


def _build_pytest_command(config: ProfilingRunConfig, coveragerc: Path) -> list[str]:
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

    if config.pytest_args:
        pytest_cmd.extend(shlex.split(config.pytest_args))

    if not supplies_cov_source(config.pytest_args):
        pytest_cmd.append(f"--cov={config.cov_source}")

    return pytest_cmd


def _run_iteration(
    pytest_cmd: list[str],
    invocation_dir: Path,
    env: dict[str, EnvVarValue],
    artefacts: HookArtefacts,
) -> IterationResult:
    """Run the suite once and collect everything that iteration left behind.

    In the INVOCATION directory, not the repository root: pytest resolves its
    rootdir, ``testpaths`` and ``pythonpath`` from the pyproject.toml it finds,
    and a project living in a subdirectory of a larger repository would
    otherwise be handed none of its own configuration.
    """
    # the command is built from sys.executable and user-provided args in a local CLI tool
    run = subprocess.run(pytest_cmd, cwd=invocation_dir, check=False, env=env)  # noqa: S603

    # The files are read whatever the exit code, both to leave the temp directory clean
    # for the next iteration and because the outcomes file is what carries the
    # collection errors -- which need reporting however pytest chose to exit.
    # Listed before anything is read, because reading consumes the files: what is
    # here is the only record of how many processes got as far as writing.
    outcomes_files = _artefact_files(artefacts.outcomes_json)
    graph_files = _artefact_files(artefacts.import_graph_json)
    read_map_files = _artefact_files(artefacts.read_map_json)
    counts = ArtefactFileCounts(
        outcomes=len(outcomes_files),
        import_graph=len(graph_files),
        read_map=len(read_map_files),
    )

    try:
        outcomes = _read_iteration_outcomes(outcomes_files)
    except OutcomesIngestError as exc:
        _fail(str(exc), run.returncode)
    try:
        import_graph = _read_iteration_graph(graph_files)
    except ImportGraphIngestError as exc:
        _fail(str(exc), run.returncode)
    try:
        read_map = _read_iteration_read_map(read_map_files)
    except ReadMapIngestError as exc:
        _fail(str(exc), run.returncode)

    return IterationResult(
        returncode=run.returncode,
        outcomes=outcomes,
        import_graph=import_graph,
        read_map=read_map,
        artefact_files=counts,
    )


def _accumulate_iterations(
    config: ProfilingRunConfig,
    pytest_cmd: list[str],
    invocation_dir: Path,
    env: dict[str, EnvVarValue],
    artefacts: HookArtefacts,
) -> SuiteObservations:
    """Run the suite as many times as configured, and merge what each pass left.

    Durations are averaged across the passes; the maps are unioned, because an
    import or a file read observed once is a fact about the codebase however many
    times it was watched for. A pass that did not finish is dropped whole rather
    than partly, so no test ends up averaged over fewer samples than its
    neighbours.
    """
    all_durations: dict[str, list[float]] = defaultdict(list)
    final_outcomes: dict[str, bool] = {}
    final_markers: dict[str, frozenset[str]] = {}
    xdist_workers = 1
    # Counted rather than taken from the config: the loop leaves early on an
    # iteration that did not finish, and the profile records how many passes its
    # durations are actually the mean of.
    completed_iterations = 0
    graphs: list[ImportGraph] = []
    read_maps: list[ReadMap] = []
    scopes: list[ProfileScope] = []
    prefixes: list[str] = []
    # The loop below always runs at least once, and every path out of it either
    # raises or has assigned this, so the initial value is never the one reported.
    returncode = int(pytest.ExitCode.OK)

    for i in range(config.iterations):
        if config.iterations > 1:
            typer.secho(f"  \U0001f504 Iteration {i + 1}/{config.iterations}...", fg=typer.colors.CYAN)

        result = _run_iteration(pytest_cmd, invocation_dir, env, artefacts)
        returncode = result.returncode

        if result.outcomes.collection_errors:
            # Fatal however many iterations have already run: the same files fail to
            # collect every time, so no amount of accumulated coverage makes up for
            # the tests that were never collected at all.
            _fail(_collection_error_message(result.outcomes.collection_errors), result.returncode)

        if result.returncode not in USABLE_EXIT_CODES:
            reason = _fatal_exit_message(result.returncode)
            # Every iteration that does not complete leaves the loop here, so i is
            # exactly the number of iterations that did complete.
            if i == 0:
                _fail(reason, result.returncode)
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

        # After the exit code, not before it: an iteration pytest already reported
        # as fatal is dropped whole above, and its missing files are explained by
        # that rather than being a second mystery. What is left here is the case
        # this guards -- pytest was content, and the artefacts still are not all
        # there.
        missing = _missing_artefact_message(result.artefact_files)
        if missing is not None:
            _fail(missing, result.returncode)

        completed_iterations += 1
        xdist_workers = max(xdist_workers, result.outcomes.xdist_workers)
        graphs.append(result.import_graph)
        read_maps.append(result.read_map)
        scopes.append(result.outcomes.scope)
        prefixes.append(result.outcomes.node_id_prefix)

        for nodeid, record in result.outcomes.outcomes.items():
            all_durations[nodeid].append(record.duration)
            # Use the last run's outcome/markers (should be consistent)
            final_outcomes[nodeid] = record.passed
            final_markers[nodeid] = frozenset(record.markers)

    try:
        node_id_prefix = _agreed_node_id_prefix(prefixes)
    except OutcomesIngestError as exc:
        _fail(str(exc), returncode)

    return SuiteObservations(
        durations={nodeid: sum(durations) / len(durations) for nodeid, durations in all_durations.items()},
        outcomes=final_outcomes,
        markers=final_markers,
        xdist_workers=xdist_workers,
        iterations=completed_iterations,
        import_graph=merge_graphs(graphs),
        scope=_merge_scopes(scopes),
        read_map=merge_read_maps(read_maps),
        node_id_prefix=node_id_prefix,
        returncode=returncode,
    )


def run_profiling(config: ProfilingRunConfig, paths: ProjectPaths) -> ProfilingRun:
    """Run the test suite under coverage instrumentation and collect results.

    Takes both roots because the run spans them: every path it records is
    relative to the repository root, while the suite itself runs in the
    invocation directory, where the project's own pytest configuration is.
    """
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

        # Before the first iteration, so the denominator names the tree the suite was
        # actually profiled against rather than whatever it became while it ran.
        present_files = _present_files(paths.repo_root)

        env = _profiling_env(temp_dir, paths, coverage_db, artefacts)
        pytest_cmd = _build_pytest_command(config, coveragerc)

        observed = _accumulate_iterations(config, pytest_cmd, paths.invocation_dir, env, artefacts)

        # Read per-test coverage straight out of coverage.py's SQLite database
        results = SuiteRunResults(
            durations=observed.durations,
            outcomes=observed.outcomes,
            markers=observed.markers,
            xdist_workers=observed.xdist_workers,
            iterations=observed.iterations,
            import_graph=observed.import_graph,
            scope=observed.scope,
            read_map=observed.read_map,
            present_files=present_files,
        )
        anchor = ProfileAnchor(project_offset=paths.project_offset, node_id_prefix=observed.node_id_prefix)
        _warn_about_an_unbounded_test_scope(results.scope)
        try:
            data = build_profiling_data(coverage_db, paths.repo_root, results, anchor, config_file=coveragerc)
        except CoverageIngestError as exc:
            _fail(str(exc), observed.returncode)

        # Fill in the missing metadata
        final_meta = ProfilingMeta(
            timestamp=datetime.now(UTC),
            commit=_get_git_commit(paths.repo_root),
            python_version=sys.version,
            coverage_version=data.meta.coverage_version,
            command=" ".join(sys.argv),
            machine=data.meta.machine,
            xdist_workers=data.meta.xdist_workers,
            iterations=data.meta.iterations,
        )

        return ProfilingRun(
            data=ProfilingData(
                meta=final_meta,
                tests=data.tests,
                total_branches=data.total_branches,
                measured_files=data.measured_files,
                import_graph=data.import_graph,
                scope=data.scope,
                anchor=data.anchor,
                unattributable_branches=data.unattributable_branches,
                reads=data.reads,
                present_files=data.present_files,
            ),
            returncode=observed.returncode,
        )
