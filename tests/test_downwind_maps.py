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
from smoke_optimiser.profiler.scope import WHOLE_REPOSITORY, ProfileScope

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
    iterations=1,
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


def test_tests_in_module_answers_for_a_test_module_coverage_never_measured() -> None:
    # The case tests_executing cannot answer at all. Under --cov=src the test
    # modules are absent from every files_covered, so the file -> tests
    # relation is empty for them; the node ids are the only record that
    # tests/test_a.py is where these two tests live.
    profile = _profile(
        tests={
            "tests/test_a.py::test_one": _outcome("tests/test_a.py::test_one", frozenset({"src/a.py"})),
            "tests/test_a.py::test_two": _outcome("tests/test_a.py::test_two", frozenset({"src/a.py"})),
            "tests/test_b.py::test_three": _outcome("tests/test_b.py::test_three", frozenset({"src/a.py"})),
        },
        measured_files=frozenset({"src/a.py"}),
        edges=frozenset({ImportEdge(importer="tests/test_a.py", imported="src/a.py")}),
        unattributed_modules=frozenset({"tests/test_a.py", "tests/test_b.py"}),
    )

    maps = DownwindMaps.from_profile(profile)

    assert maps.tests_executing("tests/test_a.py") == frozenset()
    assert maps.tests_in_module("tests/test_a.py") == frozenset(
        {"tests/test_a.py::test_one", "tests/test_a.py::test_two"}
    )
    assert maps.tests_in_module("tests/test_b.py") == frozenset({"tests/test_b.py::test_three"})


def test_a_file_defining_no_tests_is_empty_rather_than_unknown() -> None:
    # The same known-but-empty / never-seen distinction the other two maps
    # keep. Every source file and every conftest.py lands in the first case,
    # and jr5.2's unattributed rule reads exactly that difference to tell a
    # pytest-loaded test module from a module nothing was seen to import.
    profile = _profile(
        tests={"tests/test_a.py::test_one": _outcome("tests/test_a.py::test_one", frozenset({"src/a.py"}))},
        measured_files=frozenset({"src/a.py"}),
        unattributed_modules=frozenset({"conftest.py"}),
    )

    maps = DownwindMaps.from_profile(profile)

    assert maps.tests_in_module("src/a.py") == frozenset()
    assert maps.tests_in_module("conftest.py") == frozenset()
    with pytest.raises(UnknownFileError):
        maps.tests_in_module("src/never_seen.py")


def test_a_parametrised_node_id_resolves_to_its_file_and_not_its_parameters() -> None:
    # A parametrised id can carry '::' inside the brackets, so only the FIRST
    # separator delimits the path. Splitting on the last one would file this
    # test under a path that does not exist and silently lose it.
    node_id = "tests/test_a.py::test_one[a::b]"
    profile = _profile(
        tests={node_id: _outcome(node_id, frozenset({"src/a.py"}))},
        measured_files=frozenset({"src/a.py"}),
    )

    maps = DownwindMaps.from_profile(profile)

    assert maps.tests_in_module("tests/test_a.py") == frozenset({node_id})


def test_a_test_module_is_known_even_when_nothing_else_in_the_profile_mentions_it() -> None:
    # A test module that imports nothing measured reaches neither
    # measured_files nor the graph. Without its node ids in known_files the
    # maps would deny knowing the very file they hold tests for, and jr5.2
    # would refuse with UNKNOWN_PATH for a file it can answer perfectly.
    profile = _profile(
        tests={"tests/test_standalone.py::test_one": _outcome("tests/test_standalone.py::test_one", frozenset())},
        measured_files=frozenset({"src/a.py"}),
    )

    maps = DownwindMaps.from_profile(profile)

    assert maps.knows("tests/test_standalone.py")
    assert maps.tests_in_module("tests/test_standalone.py") == frozenset({"tests/test_standalone.py::test_one"})
    assert maps.dependents_of("tests/test_standalone.py") == frozenset()


def _directory_scope_profile() -> ProfilingData:
    """A suite spread across two directories, plus a source file with no tests."""
    ids = (
        "tests/sub/test_in.py::test_one",
        "tests/sub/test_in.py::test_two",
        "tests/test_outside.py::test_three",
    )
    return _profile(
        tests={test_id: _outcome(test_id, frozenset()) for test_id in ids},
        measured_files=frozenset({"src/a.py"}),
    )


def test_tests_at_or_below_a_directory_stops_at_that_directory() -> None:
    maps = DownwindMaps.from_profile(_directory_scope_profile())

    assert maps.tests_at_or_below("tests/sub") == frozenset(
        {"tests/sub/test_in.py::test_one", "tests/sub/test_in.py::test_two"}
    )


def test_tests_at_or_below_the_repository_root_is_every_recorded_test() -> None:
    # What a root conftest's directory scope comes to. WHOLE_REPOSITORY is a
    # root like any other rather than a prefix that happens to match, so the
    # rules need no special case for a conftest.py with no '/' in its path.
    maps = DownwindMaps.from_profile(_directory_scope_profile())

    assert maps.tests_at_or_below(WHOLE_REPOSITORY) == frozenset(
        {
            "tests/sub/test_in.py::test_one",
            "tests/sub/test_in.py::test_two",
            "tests/test_outside.py::test_three",
        }
    )


def test_tests_at_or_below_a_directory_holding_no_tests_answers_empty() -> None:
    # A directory is not a file the maps have an entry for, so the
    # UnknownFileError the per-file lookups raise would be wrong here: "no
    # test lives under src/" is a real answer, not a lookup miss.
    maps = DownwindMaps.from_profile(_directory_scope_profile())

    assert maps.tests_at_or_below("src") == frozenset()


def test_tests_at_or_below_does_not_match_a_sibling_sharing_a_name_prefix() -> None:
    # 'tests/sub' must not swallow 'tests/subtle': a bare string prefix test
    # would, and would select tests pytest never applies the conftest to.
    ids = ("tests/sub/test_in.py::test_one", "tests/subtle/test_other.py::test_two")
    profile = _profile(
        tests={test_id: _outcome(test_id, frozenset()) for test_id in ids},
        measured_files=frozenset(),
    )

    maps = DownwindMaps.from_profile(profile)

    assert maps.tests_at_or_below("tests/sub") == frozenset({"tests/sub/test_in.py::test_one"})
