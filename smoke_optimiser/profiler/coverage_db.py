"""Ingest of coverage.py's SQLite database.

coverage.py's ``.coverage`` file is an SQLite database whose schema is an
internal implementation detail, not a public API. We read it directly because
the public ``CoverageData`` API cannot answer "which arcs did each test
execute?" at a workable speed: ``set_query_contexts()`` costs a query per
context, which extrapolates to minutes on a medium suite.

Reading raw arcs is not enough on its own. The ``arc`` table records the
interpreter's actual jump targets, whereas coverage's reports describe branches
in an AST-derived vocabulary -- a ``for`` loop's exit arc is recorded as a jump
back to the loop header but reported as a jump to the statement after the loop.
Comparing the two vocabularies directly silently mismatches a few percent of
branches, so raw arcs are translated through coverage's own reporter, once per
file, into a lookup table that is then applied per arc.
"""

import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple

import coverage
from coverage.exceptions import NoSource
from coverage.python import PythonFileReporter

from smoke_optimiser.environment import capture_environment
from smoke_optimiser.profiler.models import (
    ProfilingData,
    ProfilingMeta,
    ProfilingOutcome,
    SuiteRunResults,
)

# Schema versions this module has been verified against. coverage.py bumps
# SCHEMA_VERSION whenever the layout changes; see coverage/sqldata.py.
SUPPORTED_SCHEMA_VERSIONS = frozenset({7})

# The module a maintainer must edit to add support for a new schema version.
SCHEMA_OWNER_MODULE = "smoke_optimiser/profiler/coverage_db.py"

# Coverage contexts recorded by ``--cov-context=test`` are node ids with a
# phase suffix, e.g. "tests/test_x.py::test_y|run".
CONTEXT_PHASE_SEPARATOR = "|"

# The context recorded for code that ran outside any test, such as imports.
NO_CONTEXT = ""

# How many unattributable contexts to name before summarising the rest.
MAX_UNKNOWN_CONTEXTS_SHOWN = 10


class CoverageIngestError(RuntimeError):
    """Raised when coverage data cannot be read reliably.

    Every failure here is fatal by design. Mis-reading coverage data would
    under-select tests without any visible symptom, which is the worst failure
    this tool can have.
    """


class Arc(NamedTuple):
    """A single line-to-line transition. Non-positive line numbers are exits."""

    fromno: int
    tono: int


@dataclass(frozen=True, eq=False)
class FileBranches:
    """The branch vocabulary of one measured file.

    Attributes:
        all_branch_ids: Every branch in the file, executed or not. This is the
            denominator: the ``arc`` table only holds branches that were taken,
            so a never-taken branch is invisible to SQLite alone.
        raw_to_branch_ids: Maps a raw recorded arc to the branch ids it counts
            towards. Raw arcs that are not branches map to nothing.
    """

    all_branch_ids: frozenset[str]
    raw_to_branch_ids: dict[Arc, frozenset[str]]


@dataclass(frozen=True)
class CoverageIngest:
    """Everything the profiler needs from one coverage database."""

    tests_branches: dict[str, frozenset[str]]
    total_branches: frozenset[str]
    unattributable_branches: frozenset[str]
    coverage_version: str


def _branch_id(relative_path: str, arc: Arc) -> str:
    return f"{relative_path}:{arc.fromno}->{arc.tono}"


def _read_schema_version(connection: sqlite3.Connection) -> int:
    try:
        row = connection.execute("select version from coverage_schema").fetchone()
    except sqlite3.Error as exc:
        raise CoverageIngestError(
            f"Could not read the coverage_schema table from the coverage database: {exc}. "
            "The file may be corrupt or may not be a coverage.py database."
        ) from exc
    if row is None:
        raise CoverageIngestError("The coverage database has an empty coverage_schema table.")
    return int(row[0])


def _read_meta(connection: sqlite3.Connection, key: str) -> str | None:
    row = connection.execute("select value from meta where key = ?", (key,)).fetchone()
    return None if row is None else str(row[0])


def _verify_database(connection: sqlite3.Connection) -> str:
    """Check the database is one we can read, and return the version that wrote it."""
    schema_version = _read_schema_version(connection)
    writer_version = _read_meta(connection, "version") or "unknown"

    if schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        supported_versions = sorted(SUPPORTED_SCHEMA_VERSIONS)
        supported = ", ".join(str(version) for version in supported_versions)
        version_word = "version" if len(supported_versions) == 1 else "versions"
        raise CoverageIngestError(
            f"Unsupported coverage.py database schema version {schema_version} "
            f"(smoke-optimiser supports schema {version_word}: {supported}).\n"
            f"The database was written by coverage.py {writer_version}; "
            f"coverage.py {coverage.__version__} is installed.\n"
            "smoke-optimiser reads coverage.py's internal database schema, so a new schema version "
            "means smoke-optimiser itself needs updating -- this is not something you can configure "
            "around.\n"
            f"To add support, verify the new layout and extend SUPPORTED_SCHEMA_VERSIONS in {SCHEMA_OWNER_MODULE}."
        )

    if _read_meta(connection, "has_arcs") != "1":
        raise CoverageIngestError(
            "The coverage database was recorded without branch coverage, so it contains no branch data "
            "(line-only results are stored as bitmaps in the line_bits table, and the arc table is empty).\n"
            "smoke-optimiser selects tests by branch coverage and cannot work from line-only data.\n"
            "Re-run with branch coverage enabled: pass --cov-branch to pytest, or set 'branch = True' "
            "under [run] in your coverage configuration."
        )

    return writer_version


def _build_file_branches(reporter: PythonFileReporter, relative_path: str, raw_arcs: set[Arc]) -> FileBranches:
    """Translate one file's raw arcs into coverage's branch vocabulary.

    Mirrors what coverage.py's own reporting layer does in
    ``coverage.results.analysis_from_file_reporter``: translate the recorded
    arcs, rewrite self-arcs onto their only possible destination, translate
    again, then keep what is both a real arc and a branch.
    """
    possibilities = reporter.arcs()
    no_branch = reporter.no_branch_lines()
    branch_lines = {line for line, exits in reporter.exit_counts().items() if exits > 1 and line not in no_branch}

    destinations: dict[int, set[int]] = defaultdict(set)
    for fromno, tono in possibilities:
        destinations[fromno].add(tono)
    single_destination = {fromno: next(iter(tonos)) for fromno, tonos in destinations.items() if len(tonos) == 1}

    raw_to_branch_ids: dict[Arc, frozenset[str]] = {}
    for raw in raw_arcs:
        rewritten = set()
        for fromno, tono in reporter.translate_arcs([raw]):
            if fromno != tono:
                rewritten.add((fromno, tono))
            elif fromno in single_destination:
                rewritten.add((fromno, single_destination[fromno]))

        branch_ids = frozenset(
            _branch_id(relative_path, Arc(fromno, tono))
            for fromno, tono in reporter.translate_arcs(rewritten)
            if (fromno, tono) in possibilities and fromno in branch_lines
        )
        if branch_ids:
            raw_to_branch_ids[raw] = branch_ids

    all_branch_ids = frozenset(
        _branch_id(relative_path, Arc(fromno, tono)) for fromno, tono in possibilities if fromno in branch_lines
    )
    return FileBranches(all_branch_ids=all_branch_ids, raw_to_branch_ids=raw_to_branch_ids)


def _test_id_from_context(context: str) -> str:
    """Strip the phase suffix, so setup/call/teardown coverage lands on one test."""
    return context.rsplit(CONTEXT_PHASE_SEPARATOR, maxsplit=1)[0]


def _verify_contexts(context_test_ids: set[str], test_durations: dict[str, float]) -> None:
    unknown = sorted(context_test_ids - set(test_durations))
    if not unknown:
        return
    shown = "\n".join(f"  {test_id}" for test_id in unknown[:MAX_UNKNOWN_CONTEXTS_SHOWN])
    hidden = len(unknown) - MAX_UNKNOWN_CONTEXTS_SHOWN
    more = f"\n  ... and {hidden} more" if hidden > 0 else ""
    counted = "1 coverage context matches" if len(unknown) == 1 else f"{len(unknown)} coverage contexts match"
    raise CoverageIngestError(
        f"{counted} no test that pytest collected in this run:\n"
        f"{shown}{more}\n"
        "Their coverage cannot be attributed to a test, which would silently shrink the smoke suite. "
        "This usually means the coverage database is stale, or was combined across different runs; "
        "remove it and profile again."
    )


def _relative_path(path: Path, project_root: Path) -> str:
    """Path relative to the project root, so profiles survive a change of machine."""
    try:
        return path.relative_to(project_root).as_posix()
    except ValueError:
        return path.as_posix()


def read_coverage_db(
    coverage_db_path: Path,
    project_root: Path,
    test_durations: dict[str, float],
    config_file: Path | None = None,
) -> CoverageIngest:
    """Read per-test branch coverage straight out of coverage.py's SQLite database."""
    if not coverage_db_path.exists():
        raise CoverageIngestError(f"No coverage database at {coverage_db_path}. The profiling run produced no data.")

    cov = coverage.Coverage(config_file=str(config_file) if config_file is not None else False)
    connection = sqlite3.connect(f"file:{coverage_db_path}?mode=ro", uri=True)
    try:
        coverage_version = _verify_database(connection)

        measured_files = {int(file_id): str(path) for file_id, path in connection.execute("select id, path from file")}
        context_names = {
            int(ctx_id): str(name) for ctx_id, name in connection.execute("select id, context from context")
        }

        raw_arcs_by_file: dict[int, set[Arc]] = defaultdict(set)
        for file_id, fromno, tono in connection.execute("select distinct file_id, fromno, tono from arc"):
            raw_arcs_by_file[int(file_id)].add(Arc(int(fromno), int(tono)))

        branches_by_file: dict[int, FileBranches] = {}
        total_branches: set[str] = set()
        for file_id, absolute_path in measured_files.items():
            reporter = PythonFileReporter(absolute_path, cov)
            relative_path = _relative_path(Path(absolute_path), project_root)
            try:
                branches = _build_file_branches(reporter, relative_path, raw_arcs_by_file.get(file_id, set()))
            except NoSource as exc:
                raise CoverageIngestError(
                    f"The coverage database refers to {absolute_path}, but its source is no longer readable: {exc}\n"
                    "Branch data cannot be derived without the source, and guessing would misreport coverage. "
                    "This usually means the file was moved or removed while profiling was running, or that the "
                    "coverage database is left over from an earlier state of the tree; profile again."
                ) from exc
            branches_by_file[file_id] = branches
            total_branches |= branches.all_branch_ids

        context_test_ids = {
            ctx_id: _test_id_from_context(name) for ctx_id, name in context_names.items() if name != NO_CONTEXT
        }
        _verify_contexts(set(context_test_ids.values()), test_durations)

        tests_branches: dict[str, set[str]] = defaultdict(set)
        unattributable: set[str] = set()
        for context_id, file_id, fromno, tono in connection.execute(
            "select context_id, file_id, fromno, tono from arc"
        ):
            branch_ids = branches_by_file[int(file_id)].raw_to_branch_ids.get(Arc(int(fromno), int(tono)))
            if branch_ids is None:
                continue
            test_id = context_test_ids.get(int(context_id))
            if test_id is None:
                unattributable |= branch_ids
            else:
                tests_branches[test_id] |= branch_ids
    finally:
        connection.close()

    covered_by_tests = set().union(*tests_branches.values()) if tests_branches else set()

    return CoverageIngest(
        tests_branches={test_id: frozenset(branches) for test_id, branches in tests_branches.items()},
        total_branches=frozenset(total_branches),
        unattributable_branches=frozenset(unattributable - covered_by_tests),
        coverage_version=coverage_version,
    )


def build_profiling_data(
    coverage_db_path: Path,
    project_root: Path,
    results: SuiteRunResults,
    config_file: Path | None = None,
) -> ProfilingData:
    """Read coverage data and assemble it into profiling results."""
    ingest = read_coverage_db(coverage_db_path, project_root, results.durations, config_file)

    tests = {
        test_id: ProfilingOutcome(
            test_id=test_id,
            duration_s=duration,
            passed=results.outcomes.get(test_id, False),
            branches_covered=ingest.tests_branches.get(test_id, frozenset()),
            markers=results.markers.get(test_id, frozenset()),
        )
        for test_id, duration in results.durations.items()
    }

    meta = ProfilingMeta(
        timestamp=datetime.now(UTC),
        commit=None,
        python_version=sys.version,
        coverage_version=ingest.coverage_version,
        command=" ".join(sys.argv),
        machine=capture_environment(),
    )

    return ProfilingData(
        meta=meta,
        tests=tests,
        total_branches=ingest.total_branches,
        unattributable_branches=ingest.unattributable_branches,
    )
