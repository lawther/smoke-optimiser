from datetime import UTC, datetime

import pytest

from smoke_optimiser.downwind.maps import DownwindMaps, UnknownFileError
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


def _outcome(test_id: str, files_covered: frozenset[str]) -> ProfilingOutcome:
    return ProfilingOutcome(
        test_id=test_id,
        duration_s=0.1,
        passed=True,
        branches_covered=frozenset(),
        files_covered=files_covered,
        markers=frozenset(),
    )


def _profile(
    tests: dict[str, ProfilingOutcome],
    measured_files: frozenset[str],
    edges: frozenset[ImportEdge] = frozenset(),
    unattributed_modules: frozenset[str] = frozenset(),
    resolution_errors: int = 0,
) -> ProfilingData:
    return ProfilingData(
        meta=_META,
        tests=tests,
        total_branches=frozenset(),
        measured_files=measured_files,
        scope=ProfileScope(
            coverage_roots=frozenset({"."}),
            test_roots=frozenset({"."}),
            test_file_patterns=("test_*.py",),
        ),
        import_graph=ImportGraph(
            edges=edges,
            unattributed_modules=unattributed_modules,
            resolution_errors=resolution_errors,
            error_samples=(),
        ),
    )


def test_file_executed_by_three_tests_maps_to_exactly_those_three() -> None:
    profile = _profile(
        tests={
            "t1": _outcome("t1", frozenset({"src/a.py"})),
            "t2": _outcome("t2", frozenset({"src/a.py"})),
            "t3": _outcome("t3", frozenset({"src/a.py"})),
            "t4": _outcome("t4", frozenset({"src/b.py"})),
        },
        measured_files=frozenset({"src/a.py", "src/b.py"}),
    )

    maps = DownwindMaps.from_profile(profile)

    assert maps.tests_executing("src/a.py") == frozenset({"t1", "t2", "t3"})


def test_transitive_import_puts_the_root_file_in_a_distant_dependents_closure() -> None:
    # test.py -> mid.py -> leaf.py: a change to leaf.py must be visible in
    # test.py's dependency closure even though test.py never imports it directly.
    profile = _profile(
        tests={"tests/test_mod.py::test_it": _outcome("tests/test_mod.py::test_it", frozenset())},
        measured_files=frozenset({"src/leaf.py", "src/mid.py", "tests/test_mod.py"}),
        edges=frozenset(
            {
                ImportEdge(importer="src/mid.py", imported="src/leaf.py"),
                ImportEdge(importer="tests/test_mod.py", imported="src/mid.py"),
            }
        ),
        unattributed_modules=frozenset({"tests/test_mod.py"}),
    )

    maps = DownwindMaps.from_profile(profile)

    assert maps.dependents_of("src/leaf.py") == frozenset({"src/mid.py", "tests/test_mod.py"})


def test_import_cycle_terminates_and_both_members_see_each_other() -> None:
    profile = _profile(
        tests={},
        measured_files=frozenset({"src/a.py", "src/b.py"}),
        edges=frozenset(
            {
                ImportEdge(importer="src/a.py", imported="src/b.py"),
                ImportEdge(importer="src/b.py", imported="src/a.py"),
            }
        ),
    )

    maps = DownwindMaps.from_profile(profile)

    assert maps.dependents_of("src/a.py") == frozenset({"src/a.py", "src/b.py"})
    assert maps.dependents_of("src/b.py") == frozenset({"src/a.py", "src/b.py"})


def test_measured_but_untested_file_is_known_with_no_tests() -> None:
    profile = _profile(
        tests={"t1": _outcome("t1", frozenset({"src/a.py"}))},
        measured_files=frozenset({"src/a.py", "src/untested.py"}),
    )

    maps = DownwindMaps.from_profile(profile)

    assert maps.knows("src/untested.py")
    assert maps.tests_executing("src/untested.py") == frozenset()


def test_unknown_file_raises_rather_than_answering_empty() -> None:
    profile = _profile(tests={}, measured_files=frozenset({"src/a.py"}))

    maps = DownwindMaps.from_profile(profile)

    assert not maps.knows("src/never_seen.py")
    with pytest.raises(UnknownFileError):
        maps.tests_executing("src/never_seen.py")
    with pytest.raises(UnknownFileError):
        maps.dependents_of("src/never_seen.py")


def test_known_files_unions_measured_graph_and_unattributed() -> None:
    profile = _profile(
        tests={},
        measured_files=frozenset({"src/a.py"}),
        edges=frozenset({ImportEdge(importer="tests/test_a.py", imported="src/a.py")}),
        unattributed_modules=frozenset({"tests/test_a.py", "conftest.py"}),
    )

    maps = DownwindMaps.from_profile(profile)

    assert maps.knows("src/a.py")
    assert maps.knows("tests/test_a.py")
    assert maps.knows("conftest.py")
    assert not maps.knows("src/nowhere.py")


def test_file_with_no_dependents_has_an_empty_but_known_closure() -> None:
    profile = _profile(
        tests={},
        measured_files=frozenset({"src/leaf.py"}),
    )

    maps = DownwindMaps.from_profile(profile)

    assert maps.knows("src/leaf.py")
    assert maps.dependents_of("src/leaf.py") == frozenset()


def test_unattributed_file_is_known_but_distinguishable_from_an_attributed_one() -> None:
    # The blind spot jr5.2 has to catch: conftest.py is known (knows() folds
    # unattributed modules in on purpose) and its dependents closure is empty
    # -- exactly like src/leaf.py, which really has no dependents. Only
    # is_unattributed separates "the graph says nothing imports this" from
    # "the graph never saw who imports this".
    profile = _profile(
        tests={},
        measured_files=frozenset({"src/leaf.py"}),
        edges=frozenset({ImportEdge(importer="tests/test_a.py", imported="src/leaf.py")}),
        unattributed_modules=frozenset({"tests/test_a.py", "conftest.py"}),
    )

    maps = DownwindMaps.from_profile(profile)

    assert maps.knows("conftest.py")
    assert maps.dependents_of("conftest.py") == frozenset()
    assert maps.is_unattributed("conftest.py")

    assert maps.dependents_of("src/leaf.py") == frozenset({"tests/test_a.py"})
    assert not maps.is_unattributed("src/leaf.py")


def test_is_unattributed_raises_for_a_file_the_maps_never_saw() -> None:
    # False would say "known, and the graph did attribute it", which is the
    # conflation UnknownFileError exists to prevent.
    profile = _profile(tests={}, measured_files=frozenset({"src/a.py"}))

    maps = DownwindMaps.from_profile(profile)

    with pytest.raises(UnknownFileError):
        maps.is_unattributed("src/never_seen.py")


_SWALLOWED_EDGES = 3


def test_resolution_errors_survive_the_inversion_as_a_count() -> None:
    # A degraded graph looks identical to a healthy one through the maps
    # themselves -- the missing edges are missing. The count is the only
    # evidence that src/a.py's empty closure might be wrong.
    degraded = _profile(
        tests={},
        measured_files=frozenset({"src/a.py"}),
        resolution_errors=_SWALLOWED_EDGES,
    )
    healthy = _profile(
        tests={},
        measured_files=frozenset({"src/a.py"}),
    )

    assert DownwindMaps.from_profile(degraded).resolution_errors == _SWALLOWED_EDGES
    assert DownwindMaps.from_profile(healthy).resolution_errors == 0
