"""Tests for the direct ingest of coverage.py's SQLite database.

Coverage databases are built here with coverage.py's *public* CoverageData
write API rather than by tracing real code. That keeps the fixtures exact (we
state precisely which arcs were recorded) and avoids nesting a second coverage
tracer inside the one already measuring this test suite.
"""

import sqlite3
from pathlib import Path

import pytest
from coverage.sqldata import CoverageData

from smoke_optimiser.profiler.coverage_db import (
    CoverageIngestError,
    build_profiling_data,
    read_coverage_db,
)
from smoke_optimiser.profiler.models import ImportGraph, ReadMap, SuiteRunResults
from smoke_optimiser.profiler.scope import ProfileScope

TEST_POS = "tests/test_app.py::test_pos"
TEST_NEG = "tests/test_app.py::test_neg"

# A source file with four branches: 2->3, 2->4 (from the first if) and
# 4->5, 4->6 (from the second). Line 6 is the fall-through return.
APP_SOURCE = """\
def classify(x):
    if x > 0:
        return "pos"
    if x < 0:
        return "neg"
    return "zero"
"""

# A file of straight-line code: no branch anywhere in it, so it contributes
# nothing to the branch vocabulary and cannot be found through branch ids.
CONSTANTS_SOURCE = """\
NAME = "app"
VALUES = (1, 2, 3)


def label():
    return NAME
"""

EXPECTED_TOTAL_BRANCHES = 4
EXPECTED_TEST_COUNT = 2


def _write_app(tmp_path: Path) -> Path:
    app = tmp_path / "app.py"
    app.write_text(APP_SOURCE)
    return app


def _write_db(db_path: Path, arcs_by_context: dict[str, dict[str, set[tuple[int, int]]]]) -> None:
    """Create a coverage database recording the given arcs per context."""
    data = CoverageData(basename=str(db_path))
    for context, file_arcs in arcs_by_context.items():
        data.set_context(context)
        data.add_arcs(file_arcs)
    data.write()


def _standard_db(tmp_path: Path) -> Path:
    """A database where test_pos took 2->3 and test_neg took 2->4 then 4->5.

    Branch 4->6 is never taken, so it has no row at all -- it exists only in
    the source, which is exactly why the denominator cannot come from SQL.
    """
    app = _write_app(tmp_path)
    db_path = tmp_path / ".coverage"
    _write_db(
        db_path,
        {
            f"{TEST_POS}|run": {str(app): {(-1, 1), (1, 2), (2, 3), (3, -1)}},
            f"{TEST_NEG}|run": {str(app): {(-1, 1), (1, 2), (2, 4), (4, 5), (5, -1)}},
        },
    )
    return db_path


def _durations() -> dict[str, float]:
    return {TEST_POS: 0.1, TEST_NEG: 0.2}


def test_attributes_branches_to_the_tests_that_took_them(tmp_path: Path) -> None:
    ingest = read_coverage_db(_standard_db(tmp_path), tmp_path, _durations())

    assert ingest.tests_branches[TEST_POS] == frozenset(["app.py:2->3"])
    assert ingest.tests_branches[TEST_NEG] == frozenset(["app.py:2->4", "app.py:4->5"])


def test_denominator_includes_branches_no_test_ever_took(tmp_path: Path) -> None:
    # 4->6 was never executed so has no arc row; counting from SQL alone would
    # report 100% coverage of three branches instead of 75% of four.
    ingest = read_coverage_db(_standard_db(tmp_path), tmp_path, _durations())

    assert ingest.total_branches == frozenset(["app.py:2->3", "app.py:2->4", "app.py:4->5", "app.py:4->6"])
    assert len(ingest.total_branches) == EXPECTED_TOTAL_BRANCHES


def test_non_branch_arcs_are_not_counted_as_branches(tmp_path: Path) -> None:
    # (1, 2) and (3, -1) are real recorded arcs but line 1 and line 3 have a
    # single exit each, so neither is a branch.
    ingest = read_coverage_db(_standard_db(tmp_path), tmp_path, _durations())

    covered = set().union(*ingest.tests_branches.values())
    assert covered <= ingest.total_branches
    assert not any(bid.startswith(("app.py:1->", "app.py:3->")) for bid in covered)


def test_paths_are_relative_to_the_project_root(tmp_path: Path) -> None:
    ingest = read_coverage_db(_standard_db(tmp_path), tmp_path, _durations())

    assert all(bid.startswith("app.py:") for bid in ingest.total_branches)


def test_setup_and_teardown_coverage_lands_on_the_same_test(tmp_path: Path) -> None:
    app = _write_app(tmp_path)
    db_path = tmp_path / ".coverage"
    _write_db(
        db_path,
        {
            f"{TEST_POS}|setup": {str(app): {(2, 3)}},
            f"{TEST_POS}|run": {str(app): {(2, 4)}},
            f"{TEST_POS}|teardown": {str(app): {(4, 5)}},
        },
    )

    ingest = read_coverage_db(db_path, tmp_path, {TEST_POS: 0.1})

    assert ingest.tests_branches[TEST_POS] == frozenset(["app.py:2->3", "app.py:2->4", "app.py:4->5"])


def test_arcs_are_translated_into_coverage_reporting_vocabulary(tmp_path: Path) -> None:
    """The arc table records jump targets, which are not what coverage reports.

    Here the loop exit physically jumps to line 6, the middle of a multi-line
    return statement, but coverage reports the branch as 3->5, the statement's
    first line. Matching raw arcs against reported branches without translating
    would drop this branch entirely -- and dropping branches silently shrinks
    the smoke suite, which is the failure this ingest exists to prevent.
    """
    source = tmp_path / "multi.py"
    source.write_text(
        "def total(xs):\n    n = 0\n    for x in xs:\n        n += x\n    return (\n        n\n        + 0\n    )\n",
    )
    db_path = tmp_path / ".coverage"
    _write_db(db_path, {f"{TEST_POS}|run": {str(source): {(2, 3), (3, 4), (4, 3), (3, 6), (6, 7), (7, 6), (5, -1)}}})

    ingest = read_coverage_db(db_path, tmp_path, {TEST_POS: 0.1})

    assert ingest.tests_branches[TEST_POS] == frozenset(["multi.py:3->4", "multi.py:3->5"])
    assert ingest.total_branches == frozenset(["multi.py:3->4", "multi.py:3->5"])


def test_import_time_branches_are_reported_as_unattributable(tmp_path: Path) -> None:
    app = _write_app(tmp_path)
    db_path = tmp_path / ".coverage"
    # The empty context is what coverage records for code running outside any test.
    _write_db(
        db_path,
        {
            "": {str(app): {(2, 3)}},
            f"{TEST_POS}|run": {str(app): {(2, 4)}},
        },
    )

    ingest = read_coverage_db(db_path, tmp_path, {TEST_POS: 0.1})

    assert ingest.unattributable_branches == frozenset(["app.py:2->3"])
    assert ingest.tests_branches[TEST_POS] == frozenset(["app.py:2->4"])


def test_a_branch_a_test_also_covers_is_not_unattributable(tmp_path: Path) -> None:
    app = _write_app(tmp_path)
    db_path = tmp_path / ".coverage"
    _write_db(db_path, {"": {str(app): {(2, 3)}}, f"{TEST_POS}|run": {str(app): {(2, 3)}}})

    ingest = read_coverage_db(db_path, tmp_path, {TEST_POS: 0.1})

    assert ingest.unattributable_branches == frozenset()


def test_unsupported_schema_version_fails_loudly(tmp_path: Path) -> None:
    db_path = _standard_db(tmp_path)
    connection = sqlite3.connect(db_path)
    connection.execute("update coverage_schema set version = 99")
    connection.commit()
    connection.close()

    with pytest.raises(CoverageIngestError) as exc_info:
        read_coverage_db(db_path, tmp_path, _durations())

    message = str(exc_info.value)
    assert "99" in message, "the schema version found must be named"
    assert "7" in message, "the supported schema versions must be named"
    assert "smoke_optimiser/profiler/coverage_db.py" in message, "the file to edit must be named"
    assert "coverage.py" in message


def test_line_only_coverage_is_refused(tmp_path: Path) -> None:
    app = _write_app(tmp_path)
    db_path = tmp_path / ".coverage"
    data = CoverageData(basename=str(db_path))
    data.set_context(f"{TEST_POS}|run")
    data.add_lines({str(app): [1, 2, 3]})
    data.write()

    with pytest.raises(CoverageIngestError, match="branch"):
        read_coverage_db(db_path, tmp_path, {TEST_POS: 0.1})


def test_context_matching_no_collected_test_fails_loudly(tmp_path: Path) -> None:
    db_path = _standard_db(tmp_path)

    with pytest.raises(CoverageIngestError) as exc_info:
        read_coverage_db(db_path, tmp_path, {TEST_POS: 0.1})

    assert TEST_NEG in str(exc_info.value)


def test_an_unattributable_context_names_every_cause_rather_than_asserting_one(tmp_path: Path) -> None:
    """The symptom has several causes and the reader has to be told which fix is theirs.

    Nothing at this point in ingest can tell a killed test from a stale
    database (so-n6b.45), so the message must not pick one. The failure this
    guards against is the message quietly reverting to a single confident
    diagnosis, which is what it used to do -- it blamed a stale database and
    told the reader to delete it, advice that reproduces the failure when the
    real cause was a deterministic timeout.
    """
    db_path = _standard_db(tmp_path)

    with pytest.raises(CoverageIngestError) as exc_info:
        read_coverage_db(db_path, tmp_path, {TEST_POS: 0.1})

    message = str(exc_info.value)
    assert TEST_NEG in message, "the unattributable context must be named"
    # Each cause has to be recognisable to someone who is living it.
    assert "killed" in message, "a killed test process must be offered as a cause"
    assert "--iterations" in message, "an abandoned iteration must be offered as a cause"
    assert "combined across runs" in message, "a stale or combined database must be offered as a cause"
    # The old advice, now attached to the one cause it actually fixes rather
    # than to the error as a whole.
    assert "usually means" not in message, "the message must not assert one cause over the others"


def test_missing_database_fails_loudly(tmp_path: Path) -> None:
    with pytest.raises(CoverageIngestError, match="No coverage database"):
        read_coverage_db(tmp_path / "absent", tmp_path, {})


def test_missing_source_fails_loudly(tmp_path: Path) -> None:
    db_path = _standard_db(tmp_path)
    (tmp_path / "app.py").unlink()

    with pytest.raises(CoverageIngestError, match="no longer readable"):
        read_coverage_db(db_path, tmp_path, _durations())


def test_a_measured_file_that_is_not_python_is_treated_as_branchless(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A Jinja2 template compiled with its own filename can end up "measured".

    Jinja2 points a compiled template's code object at the template's own
    path so its tracebacks read naturally, which makes coverage.py think it
    measured a Python file. It never contained Python branches to begin
    with, so ingest treats it as measured but branchless rather than
    failing the whole profile over it, and warns instead.
    """
    template = tmp_path / "dashboard.html"
    template.write_text("<h1>{{ title }}</h1>\n")
    db_path = tmp_path / ".coverage"
    _write_db(db_path, {f"{TEST_POS}|run": {str(template): {(-1, 1), (1, -1)}}})

    ingest = read_coverage_db(db_path, tmp_path, _durations())

    assert "dashboard.html" in ingest.measured_files
    assert ingest.total_branches == frozenset()
    assert "dashboard.html" in ingest.tests_files[TEST_POS]
    stderr = capsys.readouterr().err
    assert "dashboard.html" in stderr
    assert "not Python source" in stderr


def test_build_profiling_data_carries_outcomes_and_markers(
    tmp_path: Path, empty_graph: ImportGraph, empty_read_map: ReadMap
) -> None:
    db_path = _standard_db(tmp_path)
    results = SuiteRunResults(
        durations=_durations(),
        outcomes={TEST_POS: True, TEST_NEG: False},
        markers={TEST_POS: frozenset(["unit"]), TEST_NEG: frozenset()},
        xdist_workers=1,
        iterations=1,
        import_graph=empty_graph,
        read_map=empty_read_map,
        present_files=frozenset(),
        scope=ProfileScope(
            coverage_roots=frozenset({"src"}),
            test_roots=frozenset({"tests"}),
            test_file_patterns=("test_*.py",),
        ),
    )

    data = build_profiling_data(db_path, tmp_path, results)

    assert len(data.tests) == EXPECTED_TEST_COUNT
    assert data.tests[TEST_POS].passed is True
    assert data.tests[TEST_NEG].passed is False
    assert data.tests[TEST_POS].markers == frozenset(["unit"])
    assert data.tests[TEST_POS].duration_s == pytest.approx(0.1)
    assert data.tests[TEST_POS].branches_covered == frozenset(["app.py:2->3"])
    assert data.tests[TEST_POS].files_covered == frozenset(["app.py"])
    assert data.measured_files == frozenset(["app.py"])
    assert len(data.total_branches) == EXPECTED_TOTAL_BRANCHES
    assert data.meta.coverage_version != "unknown"


def test_a_test_with_no_recorded_coverage_still_appears(
    tmp_path: Path, empty_graph: ImportGraph, empty_read_map: ReadMap
) -> None:
    app = _write_app(tmp_path)
    db_path = tmp_path / ".coverage"
    _write_db(db_path, {f"{TEST_POS}|run": {str(app): {(2, 3)}}})
    results = SuiteRunResults(
        durations={TEST_POS: 0.1, TEST_NEG: 0.2},
        outcomes={TEST_POS: True, TEST_NEG: True},
        markers={},
        xdist_workers=1,
        iterations=1,
        import_graph=empty_graph,
        read_map=empty_read_map,
        present_files=frozenset(),
        scope=ProfileScope(
            coverage_roots=frozenset({"src"}),
            test_roots=frozenset({"tests"}),
            test_file_patterns=("test_*.py",),
        ),
    )

    data = build_profiling_data(db_path, tmp_path, results)

    assert data.tests[TEST_NEG].branches_covered == frozenset()


def test_a_file_that_is_not_a_coverage_database_fails_loudly(tmp_path: Path) -> None:
    not_a_db = tmp_path / ".coverage"
    not_a_db.write_text("this is not an SQLite database")

    with pytest.raises(CoverageIngestError, match="coverage_schema"):
        read_coverage_db(not_a_db, tmp_path, {})


def test_an_empty_schema_table_fails_loudly(tmp_path: Path) -> None:
    db_path = _standard_db(tmp_path)
    connection = sqlite3.connect(db_path)
    connection.execute("delete from coverage_schema")
    connection.commit()
    connection.close()

    with pytest.raises(CoverageIngestError, match="empty coverage_schema"):
        read_coverage_db(db_path, tmp_path, _durations())


def test_files_outside_the_project_root_keep_their_full_path(tmp_path: Path) -> None:
    # A dependency measured from site-packages has no sensible relative path,
    # so it stays absolute rather than becoming a misleading '../..' chain.
    outside = tmp_path / "outside"
    outside.mkdir()
    app = outside / "app.py"
    app.write_text(APP_SOURCE)
    project_root = tmp_path / "project"
    project_root.mkdir()
    db_path = project_root / ".coverage"
    _write_db(db_path, {f"{TEST_POS}|run": {str(app): {(2, 3)}}})

    ingest = read_coverage_db(db_path, project_root, {TEST_POS: 0.1})

    assert ingest.tests_branches[TEST_POS] == frozenset([f"{app.as_posix()}:2->3"])


def test_a_branchless_file_is_still_attributed_to_the_tests_that_ran_it(tmp_path: Path) -> None:
    """The point of the per-test file set.

    constants.py has no branches at all, so it produces no branch ids and is
    invisible to tests_branches. Asking which tests ran the file must still
    answer, or change-based selection has to fall back to the whole suite on
    an ordinary edit to a constants or model module.
    """
    constants = tmp_path / "constants.py"
    constants.write_text(CONSTANTS_SOURCE)
    db_path = tmp_path / ".coverage"
    _write_db(db_path, {f"{TEST_POS}|run": {str(constants): {(-1, 1), (1, 2), (2, 5), (-5, 6), (6, -5)}}})

    ingest = read_coverage_db(db_path, tmp_path, {TEST_POS: 0.1})

    assert ingest.total_branches == frozenset(), "the file genuinely has no branches"
    assert ingest.tests_branches.get(TEST_POS, frozenset()) == frozenset()
    assert ingest.tests_files[TEST_POS] == frozenset(["constants.py"])


def test_setup_and_teardown_files_land_on_the_same_test(tmp_path: Path) -> None:
    app = _write_app(tmp_path)
    constants = tmp_path / "constants.py"
    constants.write_text(CONSTANTS_SOURCE)
    db_path = tmp_path / ".coverage"
    _write_db(
        db_path,
        {
            f"{TEST_POS}|setup": {str(constants): {(1, 2)}},
            f"{TEST_POS}|run": {str(app): {(2, 3)}},
        },
    )

    ingest = read_coverage_db(db_path, tmp_path, {TEST_POS: 0.1})

    assert ingest.tests_files[TEST_POS] == frozenset(["app.py", "constants.py"])


def test_a_file_only_executed_at_import_time_is_measured_but_untested(tmp_path: Path) -> None:
    """'No test runs this file' must be distinguishable from 'never seen it'.

    Without measured_files both look identical -- absent -- so a changed file
    that no test executes would be reported as new code.
    """
    app = _write_app(tmp_path)
    constants = tmp_path / "constants.py"
    constants.write_text(CONSTANTS_SOURCE)
    db_path = tmp_path / ".coverage"
    _write_db(
        db_path,
        {
            "": {str(constants): {(1, 2)}},
            f"{TEST_POS}|run": {str(app): {(2, 3)}},
        },
    )

    ingest = read_coverage_db(db_path, tmp_path, {TEST_POS: 0.1})

    assert ingest.measured_files == frozenset(["app.py", "constants.py"])
    assert ingest.tests_files[TEST_POS] == frozenset(["app.py"])


def test_files_outside_the_project_root_keep_their_full_path_in_the_file_set(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    app = outside / "app.py"
    app.write_text(APP_SOURCE)
    project_root = tmp_path / "project"
    project_root.mkdir()
    db_path = project_root / ".coverage"
    _write_db(db_path, {f"{TEST_POS}|run": {str(app): {(2, 3)}}})

    ingest = read_coverage_db(db_path, project_root, {TEST_POS: 0.1})

    assert ingest.tests_files[TEST_POS] == frozenset([app.as_posix()])
