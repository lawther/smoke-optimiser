"""Unit tests for the downwind rules, over hand-built profiles.

The arithmetic here is where a rule quietly wins over another and a test is
silently lost, so every rule gets a test on its own and every pair that can
match the same input gets one for the interaction. Profiles are built by
hand rather than by profiling something, so each test states exactly the
shape of map it is about.
"""

from datetime import UTC, datetime

import pytest

from smoke_optimiser.downwind.blind_spots import BlindSpot, BlindSpotReason, in_report_order
from smoke_optimiser.downwind.changes import ChangedFile, ChangeKind
from smoke_optimiser.downwind.maps import DownwindMaps
from smoke_optimiser.downwind.rules import DownwindRefusal, DownwindSelection, downwind_of
from smoke_optimiser.environment import MachineEnvironment
from smoke_optimiser.profiler.models import (
    ImportEdge,
    ImportGraph,
    ProfilingData,
    ProfilingMeta,
    ProfilingOutcome,
)
from smoke_optimiser.profiler.scope import ProfileScope

_MACHINE = MachineEnvironment(
    os=None,
    os_version=None,
    platform=None,
    architecture=None,
    cpu_model=None,
    cpu_cores_physical=None,
    cpu_cores_logical=None,
    ram_total_mb=None,
    ram_available_mb=None,
    hostname=None,
)

_META = ProfilingMeta(
    timestamp=datetime(2026, 3, 2, 10, 30, 0, tzinfo=UTC),
    commit=None,
    python_version="3.12",
    coverage_version="7.0",
    command="smoke-optimiser",
    machine=_MACHINE,
    xdist_workers=1,
)

_SWALLOWED_EDGES = 3


def _outcome(test_id: str, files_covered: frozenset[str]) -> ProfilingOutcome:
    return ProfilingOutcome(
        test_id=test_id,
        duration_s=0.1,
        passed=True,
        branches_covered=frozenset(),
        files_covered=files_covered,
        markers=frozenset(),
    )


def _maps(
    tests: dict[str, frozenset[str]],
    measured_files: frozenset[str],
    edges: frozenset[ImportEdge] = frozenset(),
    unattributed_modules: frozenset[str] = frozenset(),
    resolution_errors: int = 0,
) -> DownwindMaps:
    """Maps over a profile described as node id -> the files that test executed."""
    profile = ProfilingData(
        meta=_META,
        tests={test_id: _outcome(test_id, files) for test_id, files in tests.items()},
        total_branches=frozenset(),
        measured_files=measured_files,
        scope=ProfileScope(
            coverage_roots=frozenset({"src"}),
            test_roots=frozenset({"tests"}),
            test_file_patterns=("test_*.py",),
        ),
        import_graph=ImportGraph(
            edges=edges,
            unattributed_modules=unattributed_modules,
            resolution_errors=resolution_errors,
            error_samples=(),
        ),
    )
    return DownwindMaps.from_profile(profile)


def _changed(*paths: str) -> frozenset[ChangedFile]:
    return frozenset(ChangedFile(path=path, kind=ChangeKind.MODIFIED) for path in paths)


# The profile every "ordinary answer" test reads: a --cov=src profile, so the
# test modules are measured by nothing and reach the maps only through the
# import graph. tests/test_a.py imports src/a.py, which imports
# src/declarations.py -- the import-time-only file coverage attributes to no
# test at all.
_STANDARD_TESTS = {
    "tests/test_a.py::test_one": frozenset({"src/a.py"}),
    "tests/test_a.py::test_two": frozenset({"src/a.py"}),
    "tests/test_b.py::test_three": frozenset({"src/b.py"}),
}
_STANDARD_MEASURED = frozenset({"src/a.py", "src/b.py", "src/declarations.py"})
_STANDARD_EDGES = frozenset(
    {
        ImportEdge(importer="tests/test_a.py", imported="src/a.py"),
        ImportEdge(importer="src/a.py", imported="src/declarations.py"),
        ImportEdge(importer="tests/test_b.py", imported="src/b.py"),
    }
)
_STANDARD_UNATTRIBUTED = frozenset({"tests/test_a.py", "tests/test_b.py"})


def _standard_maps(
    *,
    measured_files: frozenset[str] = _STANDARD_MEASURED,
    unattributed_modules: frozenset[str] = _STANDARD_UNATTRIBUTED,
    resolution_errors: int = 0,
) -> DownwindMaps:
    return _maps(
        tests=_STANDARD_TESTS,
        measured_files=measured_files,
        edges=_STANDARD_EDGES,
        unattributed_modules=unattributed_modules,
        resolution_errors=resolution_errors,
    )


def _all_existing() -> frozenset[str]:
    """A tree the maps know entirely, so expiry never fires incidentally."""
    return frozenset({"src/a.py", "src/b.py", "src/declarations.py", "tests/test_a.py", "tests/test_b.py"})


def test_a_changed_source_file_selects_the_tests_that_executed_it() -> None:
    answer = downwind_of(_standard_maps(), _changed("src/a.py"), _all_existing())

    assert answer == DownwindSelection(node_ids=frozenset({"tests/test_a.py::test_one", "tests/test_a.py::test_two"}))


def test_a_changed_import_time_only_file_selects_the_tests_whose_module_imports_it() -> None:
    # The case the import graph exists for. No test executed
    # src/declarations.py, so coverage alone selects nothing; the closure
    # reaches src/a.py and then tests/test_a.py, and the node ids say which
    # tests live there -- which matters because a --cov=src profile measured
    # no test module, so tests_executing() is empty for all of them.
    answer = downwind_of(_standard_maps(), _changed("src/declarations.py"), _all_existing())

    assert answer == DownwindSelection(node_ids=frozenset({"tests/test_a.py::test_one", "tests/test_a.py::test_two"}))


def test_a_changed_test_file_selects_the_tests_in_that_file() -> None:
    # tests/test_a.py is unattributed -- pytest loaded it and nothing imports
    # it -- but it defines tests, which is WHY nothing imports it. Refusing
    # here would cost a full suite for the commonest edit there is.
    answer = downwind_of(_standard_maps(), _changed("tests/test_a.py"), _all_existing())

    assert answer == DownwindSelection(node_ids=frozenset({"tests/test_a.py::test_one", "tests/test_a.py::test_two"}))


def test_a_test_module_that_others_import_is_answered_through_the_closure() -> None:
    # A shared helper that happens to live in a test file: something imports
    # it, so it carries an edge and is not unattributed at all.
    maps = _maps(
        tests={
            "tests/test_a.py::test_one": frozenset({"src/a.py"}),
            "tests/test_b.py::test_two": frozenset({"src/a.py"}),
        },
        measured_files=frozenset({"src/a.py"}),
        edges=frozenset(
            {
                ImportEdge(importer="tests/test_a.py", imported="tests/helpers.py"),
                ImportEdge(importer="tests/test_b.py", imported="tests/helpers.py"),
            }
        ),
        unattributed_modules=frozenset({"tests/test_a.py", "tests/test_b.py"}),
    )

    answer = downwind_of(maps, _changed("tests/helpers.py"), frozenset())

    assert answer == DownwindSelection(node_ids=frozenset({"tests/test_a.py::test_one", "tests/test_b.py::test_two"}))


def test_fixture_mediated_reach_is_answered_by_coverage_where_the_closure_stops() -> None:
    # A profile that DID measure the tests (--cov=.). Nothing imports
    # conftest.py, so a change to the module it imports reaches the conftest
    # and no further; the tests that ran the conftest's fixture code are the
    # only record of who used it.
    maps = _maps(
        tests={
            "tests/test_a.py::test_one": frozenset({"tests/test_a.py", "tests/conftest.py", "src/fixtures.py"}),
        },
        measured_files=frozenset({"src/fixtures.py", "tests/conftest.py", "tests/test_a.py"}),
        edges=frozenset({ImportEdge(importer="tests/conftest.py", imported="src/fixtures.py")}),
        unattributed_modules=frozenset({"tests/conftest.py", "tests/test_a.py"}),
    )

    answer = downwind_of(maps, _changed("src/fixtures.py"), frozenset())

    assert answer == DownwindSelection(node_ids=frozenset({"tests/test_a.py::test_one"}))


def test_an_empty_changed_set_selects_nothing_rather_than_refusing() -> None:
    answer = downwind_of(_standard_maps(), frozenset(), _all_existing())

    assert answer == DownwindSelection(node_ids=frozenset())


def test_a_changed_path_the_maps_never_saw_refuses_with_unknown_path() -> None:
    answer = downwind_of(_standard_maps(), _changed("src/brand_new.py"), _all_existing())

    assert answer == DownwindRefusal(
        blind_spots=frozenset({BlindSpot(reason=BlindSpotReason.UNKNOWN_PATH, file="src/brand_new.py")})
    )


def test_a_changed_non_python_path_refuses_with_non_python_file() -> None:
    # Also absent from the maps, so rule order is what decides this reason:
    # absence tells us nothing about a data file, which is a different fact
    # from a Python file we have never measured.
    answer = downwind_of(_standard_maps(), _changed("config/settings.yaml"), _all_existing())

    assert answer == DownwindRefusal(
        blind_spots=frozenset({BlindSpot(reason=BlindSpotReason.NON_PYTHON_FILE, file="config/settings.yaml")})
    )


def test_a_changed_conftest_refuses_with_changed_conftest() -> None:
    maps = _standard_maps(unattributed_modules=frozenset({"tests/test_a.py", "tests/test_b.py", "tests/conftest.py"}))

    answer = downwind_of(maps, _changed("tests/conftest.py"), _all_existing())

    assert answer == DownwindRefusal(
        blind_spots=frozenset({BlindSpot(reason=BlindSpotReason.CHANGED_CONFTEST, file="tests/conftest.py")})
    )


def test_a_changed_unattributed_module_with_no_tests_refuses() -> None:
    # A pytest11 plugin belonging to the project: loaded before the tracer
    # could watch, so nothing was seen to import it, and it defines no tests
    # to explain why. Its empty closure is the graph being blind.
    maps = _standard_maps(
        unattributed_modules=frozenset({"tests/test_a.py", "tests/test_b.py", "src/plugin.py"}),
        measured_files=frozenset({"src/a.py", "src/b.py", "src/declarations.py", "src/plugin.py"}),
    )

    answer = downwind_of(maps, _changed("src/plugin.py"), _all_existing())

    assert answer == DownwindRefusal(
        blind_spots=frozenset({BlindSpot(reason=BlindSpotReason.UNATTRIBUTED_IMPORT, file="src/plugin.py")})
    )


def test_a_file_answerable_by_coverage_but_unattributed_refuses_rather_than_answering_partially() -> None:
    # src/plugin.py WAS executed by a test, so coverage has a real answer for
    # it. It is still refused: the graph cannot say who else depends on it,
    # and a partial answer here is under-selection dressed as a selection.
    maps = _maps(
        tests={"tests/test_a.py::test_one": frozenset({"src/plugin.py"})},
        measured_files=frozenset({"src/plugin.py"}),
        unattributed_modules=frozenset({"src/plugin.py"}),
    )

    answer = downwind_of(maps, _changed("src/plugin.py"), frozenset())

    assert answer == DownwindRefusal(
        blind_spots=frozenset({BlindSpot(reason=BlindSpotReason.UNATTRIBUTED_IMPORT, file="src/plugin.py")})
    )


def test_any_resolution_errors_refuse_and_name_the_count() -> None:
    # Blunt by design: the count cannot say WHICH closure a missing edge
    # shortened, so every answer the graph gives is a possible
    # under-estimate. Refused regardless of how well the maps could otherwise
    # answer for src/a.py.
    maps = _standard_maps(resolution_errors=_SWALLOWED_EDGES)

    answer = downwind_of(maps, _changed("src/a.py"), _all_existing())

    assert answer == DownwindRefusal(
        blind_spots=frozenset({BlindSpot(reason=BlindSpotReason.RESOLUTION_ERRORS, resolution_errors=_SWALLOWED_EDGES)})
    )


def test_a_file_that_exists_but_the_maps_never_saw_expires_the_profile() -> None:
    # Every CHANGED file is answerable here. The refusal comes from a file
    # nobody touched: a teammate's test module, arrived by pull. From its
    # arrival the maps answer confidently and wrongly, since its tests are
    # absent from every answer they give.
    answer = downwind_of(
        _standard_maps(),
        _changed("src/a.py"),
        _all_existing() | {"tests/test_theirs.py"},
    )

    assert answer == DownwindRefusal(
        blind_spots=frozenset({BlindSpot(reason=BlindSpotReason.EXPIRED_PROFILE, file="tests/test_theirs.py")})
    )


def test_a_newly_added_file_reports_both_unknown_path_and_expired_profile() -> None:
    # The overlap the rules exist to make explicit. A file you added is both
    # a changed path the maps have never seen and the file that expires the
    # profile. Both are true, neither is derived from the other, and the two
    # reasons are fixed by rule order rather than by which rule ran first.
    answer = downwind_of(
        _standard_maps(),
        _changed("src/brand_new.py"),
        _all_existing() | {"src/brand_new.py"},
    )

    assert answer == DownwindRefusal(
        blind_spots=frozenset(
            {
                BlindSpot(reason=BlindSpotReason.UNKNOWN_PATH, file="src/brand_new.py"),
                BlindSpot(reason=BlindSpotReason.EXPIRED_PROFILE, file="src/brand_new.py"),
            }
        )
    )


def test_every_offending_input_contributes_its_own_blind_spot() -> None:
    # Accumulation, in a fixed order: changed paths sorted, then the whole
    # graph, then the diverged tree sorted. One run tells the developer
    # everything they would have to fix.
    maps = _standard_maps(resolution_errors=_SWALLOWED_EDGES)

    answer = downwind_of(
        maps,
        _changed("config/settings.yaml", "src/brand_new.py", "src/a.py"),
        _all_existing() | {"tests/test_theirs.py"},
    )

    assert answer == DownwindRefusal(
        blind_spots=frozenset(
            {
                BlindSpot(reason=BlindSpotReason.NON_PYTHON_FILE, file="config/settings.yaml"),
                BlindSpot(reason=BlindSpotReason.UNKNOWN_PATH, file="src/brand_new.py"),
                BlindSpot(reason=BlindSpotReason.RESOLUTION_ERRORS, resolution_errors=_SWALLOWED_EDGES),
                BlindSpot(reason=BlindSpotReason.EXPIRED_PROFILE, file="tests/test_theirs.py"),
            }
        )
    )


def test_one_answerable_file_does_not_rescue_a_refusal_caused_by_another() -> None:
    # The rules answer for the whole change or not at all: node ids selected
    # for src/a.py are discarded, because a refusal that also carried a test
    # list would be a caller's invitation to run that list instead.
    answer = downwind_of(_standard_maps(), _changed("src/a.py", "src/brand_new.py"), _all_existing())

    assert isinstance(answer, DownwindRefusal)
    assert not hasattr(answer, "node_ids")


def test_a_deleted_file_is_looked_up_exactly_like_any_other_change() -> None:
    # Nothing gates on whether the path still exists: the tests that executed
    # a file are the ones its deletion is most likely to break, and by the
    # time the rules run there is no file left to ask.
    deleted = frozenset({ChangedFile(path="src/a.py", kind=ChangeKind.DELETED)})

    answer = downwind_of(_standard_maps(), deleted, _all_existing() - {"src/a.py"})

    assert answer == DownwindSelection(node_ids=frozenset({"tests/test_a.py::test_one", "tests/test_a.py::test_two"}))


def test_one_path_reported_under_two_kinds_yields_one_blind_spot() -> None:
    # git can report the same path twice -- staged one way, unstaged another.
    # No rule reads the kind, so the path must not be refused twice for the
    # same reason.
    twice = frozenset(
        {
            ChangedFile(path="src/brand_new.py", kind=ChangeKind.ADDED),
            ChangedFile(path="src/brand_new.py", kind=ChangeKind.MODIFIED),
        }
    )

    answer = downwind_of(_standard_maps(), twice, _all_existing())

    assert answer == DownwindRefusal(
        blind_spots=frozenset({BlindSpot(reason=BlindSpotReason.UNKNOWN_PATH, file="src/brand_new.py")})
    )


def test_a_refusal_cannot_be_built_without_a_reason() -> None:
    # The type's whole job is to mean "I could not answer, and here is why".
    # An empty one would be a refusal with no reason, which is the shape a
    # caller could mistake for a selection of nothing.
    with pytest.raises(ValueError, match="at least one blind spot"):
        DownwindRefusal(blind_spots=frozenset())


def test_a_blind_spot_must_name_the_file_it_refused_for() -> None:
    # Every reason but RESOLUTION_ERRORS is about one input, and a refusal
    # that cannot say which input caused it sends the developer looking
    # through the whole changed set by hand.
    with pytest.raises(ValueError, match="file must be set"):
        BlindSpot(reason=BlindSpotReason.UNKNOWN_PATH)

    with pytest.raises(ValueError, match="file must be set"):
        BlindSpot(reason=BlindSpotReason.RESOLUTION_ERRORS, file="src/a.py", resolution_errors=1)


def test_only_a_resolution_errors_blind_spot_carries_a_count() -> None:
    # The count is what that refusal is required to name, and it means
    # nothing attached to any other reason.
    with pytest.raises(ValueError, match="resolution_errors must be set"):
        BlindSpot(reason=BlindSpotReason.RESOLUTION_ERRORS)

    with pytest.raises(ValueError, match="resolution_errors must be set"):
        BlindSpot(reason=BlindSpotReason.UNKNOWN_PATH, file="src/a.py", resolution_errors=1)


def test_blind_spots_are_reported_in_one_order_whatever_order_they_were_found_in() -> None:
    # The refusal itself is a set, so the order a reader sees has to be
    # imposed somewhere. Two identical runs must produce identical output:
    # an order that varies with set iteration is a diff nobody can account
    # for, in a file meant to be diffed.
    spots = {
        BlindSpot(reason=BlindSpotReason.UNKNOWN_PATH, file="src/brand_new.py"),
        BlindSpot(reason=BlindSpotReason.EXPIRED_PROFILE, file="tests/test_theirs.py"),
        BlindSpot(reason=BlindSpotReason.EXPIRED_PROFILE, file="src/theirs.py"),
        BlindSpot(reason=BlindSpotReason.RESOLUTION_ERRORS, resolution_errors=_SWALLOWED_EDGES),
    }

    assert in_report_order(spots) == [
        BlindSpot(reason=BlindSpotReason.EXPIRED_PROFILE, file="src/theirs.py"),
        BlindSpot(reason=BlindSpotReason.EXPIRED_PROFILE, file="tests/test_theirs.py"),
        BlindSpot(reason=BlindSpotReason.RESOLUTION_ERRORS, resolution_errors=_SWALLOWED_EDGES),
        BlindSpot(reason=BlindSpotReason.UNKNOWN_PATH, file="src/brand_new.py"),
    ]
    assert in_report_order(spots) == in_report_order(reversed(in_report_order(spots)))
