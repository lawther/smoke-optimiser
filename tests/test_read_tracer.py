"""Tests for the per-test file read and directory listing tracer.

Every test here drives ``ReadTracer.audit`` directly rather than installing the
hook and calling ``open``. An audit hook cannot be removed once installed, so a
test that installed one would leave it recording for every test that ran after
it, in the same process -- the suite would be measuring itself. The event
arguments used here are the ones CPython really passes, verified against a live
hook: ``builtins.open`` and ``pathlib`` report a mode string, ``os.open``
reports None and puts its intent in the flags.
"""

from pathlib import Path

import pytest

from smoke_optimiser.profiler.models import ReadMap
from smoke_optimiser.profiler.read_tracer import (
    ReadMapIngestError,
    ReadTracer,
    is_read_mode,
    merge_read_maps,
    read_read_map,
    write_read_map,
)

TEST_ID = "tests/test_thing.py::test_one"
OTHER_TEST_ID = "tests/test_thing.py::test_two"

EXPECTED_MERGED_ERRORS = 3

# Flags as os.open reports them, which is where its access mode lives when the
# mode string is None. O_CLOEXEC rides along on macOS and must not confuse the
# access-mode check.
O_CLOEXEC = 0o100000000
READ_FLAGS = O_CLOEXEC
WRITE_FLAGS = O_CLOEXEC | 0o1401


def _opened(path: Path, mode: str | None = "r", flags: int = READ_FLAGS) -> tuple[object, object, int]:
    """The argument tuple CPython passes with an "open" audit event."""
    return (str(path), mode, flags)


def _tracer(root: Path, *, active: str | None = TEST_ID) -> ReadTracer:
    tracer = ReadTracer(root)
    tracer.install()
    tracer.set_active_test(active)
    return tracer


def test_a_read_inside_a_test_is_attributed_to_it(tmp_path: Path) -> None:
    tracer = _tracer(tmp_path)

    tracer.audit("open", _opened(tmp_path / "fixtures/rates.yaml"))

    assert tracer.snapshot().reads_by_test == {TEST_ID: frozenset({"fixtures/rates.yaml"})}


def test_os_open_is_recorded_even_though_it_reports_no_mode(tmp_path: Path) -> None:
    """os.open bypasses builtins.open entirely, which is why the hook exists."""
    tracer = _tracer(tmp_path)

    tracer.audit("open", _opened(tmp_path / "notes.txt", mode=None, flags=READ_FLAGS))

    assert tracer.snapshot().reads_by_test == {TEST_ID: frozenset({"notes.txt"})}


@pytest.mark.parametrize(
    ("mode", "flags"),
    [("w", WRITE_FLAGS), ("a", WRITE_FLAGS), (None, WRITE_FLAGS)],
)
def test_a_write_creates_no_dependency(tmp_path: Path, mode: str | None, flags: int) -> None:
    """Writing a file says nothing about depending on what it held."""
    tracer = _tracer(tmp_path)

    tracer.audit("open", _opened(tmp_path / "output.json", mode=mode, flags=flags))

    assert tracer.snapshot().reads_by_test == {}


def test_a_read_outside_any_test_is_unattributed_rather_than_unrecorded(tmp_path: Path) -> None:
    """Import-time and collection-time reads are real dependencies with no test.

    Dropping them would make the file look unread, which selects nothing --
    the silent under-selection this whole map exists to avoid.
    """
    tracer = _tracer(tmp_path, active=None)

    tracer.audit("open", _opened(tmp_path / "config.yaml"))

    snapshot = tracer.snapshot()
    assert snapshot.unattributed_reads == frozenset({"config.yaml"})
    assert snapshot.reads_by_test == {}


def test_python_files_are_left_to_coverage_and_the_import_graph(tmp_path: Path) -> None:
    tracer = _tracer(tmp_path)

    tracer.audit("open", _opened(tmp_path / "app/loader.py"))

    assert tracer.snapshot().reads_by_test == {}


@pytest.mark.parametrize("excluded", [".venv/lib/thing.yaml", "app/__pycache__/x.json", ".git/config"])
def test_dependencies_and_caches_inside_the_root_are_excluded(tmp_path: Path, excluded: str) -> None:
    tracer = _tracer(tmp_path)

    tracer.audit("open", _opened(tmp_path / excluded))

    assert tracer.snapshot().reads_by_test == {}


def test_a_file_outside_the_repository_is_not_recorded(tmp_path: Path) -> None:
    """The standard library and site-packages are the bulk of a suite's opens."""
    outside = tmp_path.parent / "elsewhere.yaml"
    tracer = _tracer(tmp_path)

    tracer.audit("open", _opened(outside))

    assert tracer.snapshot().reads_by_test == {}


def test_a_file_descriptor_is_ignored_rather_than_counted_as_a_failure(tmp_path: Path) -> None:
    """open(fd) passes an integer, and a descriptor names no path.

    It must not reach the error count: that count means a read was LOST, which
    forces the full suite, so spending it on an event with nothing to record
    would refuse to select over a file nobody touched.
    """
    tracer = _tracer(tmp_path)

    tracer.audit("open", (7, "r", READ_FLAGS))

    snapshot = tracer.snapshot()
    assert snapshot.reads_by_test == {}
    assert snapshot.recording_errors == 0


def test_a_malformed_event_is_counted_rather_than_raised(tmp_path: Path) -> None:
    """Recording runs inside every open of the suite being profiled.

    Letting anything escape would take down the user's test run, so failures
    are swallowed -- and counted, because a swallowed failure is a read that
    went missing.
    """
    tracer = _tracer(tmp_path)

    tracer.audit("open", ())

    snapshot = tracer.snapshot()
    assert snapshot.recording_errors == 1
    assert snapshot.error_samples


@pytest.mark.parametrize("event", ["os.listdir", "os.scandir"])
def test_a_listing_inside_a_test_records_the_directory(tmp_path: Path, event: str) -> None:
    tracer = _tracer(tmp_path)

    tracer.audit(event, (str(tmp_path / "fixtures"),))

    assert tracer.snapshot().listings_by_test == {TEST_ID: frozenset({"fixtures"})}


def test_listing_the_repository_root_is_recorded_as_the_root(tmp_path: Path) -> None:
    tracer = _tracer(tmp_path)

    tracer.audit("os.scandir", (str(tmp_path),))

    assert tracer.snapshot().listings_by_test == {TEST_ID: frozenset({"."})}


def test_a_listing_outside_any_test_is_discarded(tmp_path: Path) -> None:
    """The one observation the tracer deliberately throws away.

    pytest's collection walks the test tree and importlib scans sys.path on
    every import, both under no test context. Recording those would make every
    directory in the repository look like something a test depends on, and put
    the whole tree permanently in the full-suite bucket.
    """
    tracer = _tracer(tmp_path, active=None)

    tracer.audit("os.scandir", (str(tmp_path / "tests"),))

    assert tracer.snapshot().listings_by_test == {}


def test_nothing_is_recorded_once_recording_stops(tmp_path: Path) -> None:
    """An audit hook cannot be removed, so uninstall has to stop it by flag."""
    tracer = _tracer(tmp_path)
    tracer.uninstall()

    tracer.audit("open", _opened(tmp_path / "fixtures/rates.yaml"))

    assert tracer.snapshot().reads_by_test == {}


def test_setup_and_teardown_reads_land_on_the_same_test(tmp_path: Path) -> None:
    """A fixture that reads a fixture file is a dependency of the test using it."""
    tracer = _tracer(tmp_path)

    tracer.audit("open", _opened(tmp_path / "fixtures/setup.yaml"))
    tracer.set_active_test(OTHER_TEST_ID)
    tracer.audit("open", _opened(tmp_path / "fixtures/other.yaml"))

    snapshot = tracer.snapshot()
    assert snapshot.reads_by_test[TEST_ID] == frozenset({"fixtures/setup.yaml"})
    assert snapshot.reads_by_test[OTHER_TEST_ID] == frozenset({"fixtures/other.yaml"})


@pytest.mark.parametrize(
    ("mode", "flags", "expected"),
    [
        ("r", 0, True),
        ("rb", 0, True),
        ("r+", 0, True),
        ("w+", 0, True),
        ("w", 0, False),
        ("ab", 0, False),
        ("x", 0, False),
        (None, READ_FLAGS, True),
        (None, WRITE_FLAGS, False),
        (None, O_CLOEXEC | 0o2, True),
    ],
)
def test_read_modes_are_told_apart_from_writes(mode: str | None, flags: int, *, expected: bool) -> None:
    assert is_read_mode(mode, flags) is expected


def test_a_read_map_survives_the_round_trip_to_a_file(tmp_path: Path) -> None:
    read_map = ReadMap(
        reads_by_test={TEST_ID: frozenset({"fixtures/a.yaml"})},
        listings_by_test={TEST_ID: frozenset({"fixtures"})},
        unattributed_reads=frozenset({"config.yaml"}),
        recording_errors=2,
        error_samples=("TypeError()",),
    )
    path = tmp_path / "read_map.json"

    write_read_map(read_map, path)

    assert read_read_map(path) == read_map


def test_an_unreadable_read_map_fails_loudly(tmp_path: Path) -> None:
    """A partial map looks exactly like a suite that reads fewer files than it does."""
    path = tmp_path / "read_map.json"
    path.write_text("{not json")

    with pytest.raises(ReadMapIngestError, match="under-selects"):
        read_read_map(path)


def test_worker_maps_merge_by_union() -> None:
    """Under xdist each worker sees only the reads its own slice provoked."""
    first = ReadMap(
        reads_by_test={TEST_ID: frozenset({"fixtures/a.yaml"})},
        listings_by_test={TEST_ID: frozenset({"fixtures"})},
        unattributed_reads=frozenset(),
        recording_errors=1,
        error_samples=("first",),
    )
    second = ReadMap(
        reads_by_test={TEST_ID: frozenset({"fixtures/b.yaml"}), OTHER_TEST_ID: frozenset({"fixtures/c.yaml"})},
        listings_by_test={OTHER_TEST_ID: frozenset({"golden"})},
        unattributed_reads=frozenset(),
        recording_errors=2,
        error_samples=("second",),
    )

    merged = merge_read_maps([first, second])

    assert merged.reads_by_test[TEST_ID] == frozenset({"fixtures/a.yaml", "fixtures/b.yaml"})
    assert merged.reads_by_test[OTHER_TEST_ID] == frozenset({"fixtures/c.yaml"})
    assert merged.listings_by_test[TEST_ID] == frozenset({"fixtures"})
    assert merged.listings_by_test[OTHER_TEST_ID] == frozenset({"golden"})
    assert merged.recording_errors == EXPECTED_MERGED_ERRORS


def test_a_read_one_worker_could_not_attribute_is_resolved_against_the_others() -> None:
    """One worker importing a module reads a file another worker reads inside a test.

    Unioning the unattributed sets blindly would refuse to select for that file
    on the strength of an observation another worker already answered.
    """
    importing_worker = ReadMap(
        reads_by_test={},
        listings_by_test={},
        unattributed_reads=frozenset({"fixtures/a.yaml", "config.yaml"}),
        recording_errors=0,
        error_samples=(),
    )
    running_worker = ReadMap(
        reads_by_test={TEST_ID: frozenset({"fixtures/a.yaml"})},
        listings_by_test={},
        unattributed_reads=frozenset(),
        recording_errors=0,
        error_samples=(),
    )

    merged = merge_read_maps([importing_worker, running_worker])

    assert merged.unattributed_reads == frozenset({"config.yaml"})


def test_merging_nothing_yields_an_empty_map() -> None:
    merged = merge_read_maps([])

    assert merged == ReadMap(
        reads_by_test={},
        listings_by_test={},
        unattributed_reads=frozenset(),
        recording_errors=0,
        error_samples=(),
    )
