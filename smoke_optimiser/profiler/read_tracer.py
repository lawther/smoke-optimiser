"""Record which files each test reads, and which directories it lists.

Coverage answers which tests executed a Python file, and the import graph
answers which tests depend on one. A data file -- a YAML fixture, a Jinja
template, a golden file, a .sql migration -- has neither relation, so its
absence from those maps says only that nobody looked. That is why a changed
README.md and a changed fixtures/rates.yaml are, without this module, the same
input: unanswerable, so run everything.

One ``sys.addaudithook`` supplies both relations this module records:

* the PEP 578 ``open`` event, which fires for ``builtins.open``,
  ``pathlib.Path.read_text`` and ``os.open`` alike -- one mechanism where the
  import tracer needed three patched ones. ``os.open`` reports no mode string,
  so its access mode is read off the flags instead.
* the ``os.listdir`` and ``os.scandir`` events, which record the DIRECTORIES a
  test listed. A test that globs ``fixtures/*.yaml`` never names the file it
  opens, so reads alone cannot answer for a file added after profiling: it was
  not there to be read, and its absence would read as "nothing reads this".
  ``os.scandir`` is what ``glob.glob``, ``pathlib.Path.glob`` and ``os.walk``
  all go through, so none of those needs an event of its own.

READS OUTSIDE A TEST are recorded as unattributed, exactly as the import graph
records a module nothing was seen to import. Import-time and collection-time
reads are real dependencies with no test to attribute them to, and reading them
as "no test reads this" would under-select silently.

LISTINGS OUTSIDE A TEST are DISCARDED, and this is the one place the tracer
deliberately throws an observation away. The tree is walked constantly under no
test context -- pytest's collection walks the test tree, importlib scans every
``sys.path`` entry on every import -- so treating those as dependencies would
put most of the repository permanently in the full-suite bucket, and leave the
map worthless for the commonest case of all, a new file under ``tests/``. The
price is a module-level ``glob()`` at import time: files it picks up that are
added LATER are missed. Files it already read are still safe, because reading
them lands in the unattributed set above.

Only non-Python files are recorded. Python files have two better relations
already, and recording every source file the interpreter opens would bury the
data files this exists for.

Recording must never break the suite being profiled, so failures are swallowed
and counted. A swallowed failure is a MISSING read, which would under-select
later, so the count travels in the profile.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from smoke_optimiser.profiler.models import ReadMap, ReadMapModel
from smoke_optimiser.profiler.scope import WHOLE_REPOSITORY

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

MAX_ERROR_SAMPLES = 5

OPEN_EVENT = "open"
"""PEP 578's event for every file opened, whichever call opened it."""

LISTING_EVENTS = frozenset({"os.listdir", "os.scandir"})
"""The events every directory walk goes through, ``glob`` and ``os.walk`` included.

``glob.glob`` raises an event of its own, deliberately ignored: it carries the
pattern rather than the directory scanned, and the scan itself raises
``os.scandir`` regardless.
"""

PYTHON_SUFFIX = ".py"
"""Recorded by coverage and the import graph, so not recorded again here."""

READ_MODE_CHARACTERS = frozenset({"r", "+"})
"""Mode characters that mean the caller can see what the file already held.

``w+`` truncates before anything can be read, so counting it is an
over-estimate -- which only ever adds tests to an answer.
"""

# Directories whose contents are dependencies, build artefacts or caches rather
# than project data, even when they sit inside the project root.
EXCLUDED_PATH_PARTS = frozenset(
    {
        ".venv",
        "site-packages",
        "dist-packages",
        "__pycache__",
        ".git",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "node_modules",
    }
)


def is_read_mode(mode: str | None, flags: int) -> bool:
    """Did this open let the caller see what the file already held?

    ``mode`` is the string ``open()`` was given, and None for ``os.open``,
    which reports its intent in ``flags`` instead.
    """
    if mode is None:
        return flags & os.O_ACCMODE in {os.O_RDONLY, os.O_RDWR}
    return any(character in READ_MODE_CHARACTERS for character in mode)


class ReadTracer:
    """Captures per-test file reads and directory listings for one process.

    The active test is set by the profiling hook around each test's protocol,
    so setup, call and teardown all attribute to the same node id -- matching
    how coverage contexts are recorded, and how a fixture that reads a fixture
    file is a dependency of the test that used it.

    An audit hook cannot be removed once installed, so :meth:`uninstall` stops
    recording by flag rather than by removal.
    """

    def __init__(self, project_root: Path) -> None:
        self._root = Path(project_root).resolve()
        self._root_prefix = str(self._root)
        self._reads: dict[str, set[str]] = {}
        self._listings: dict[str, set[str]] = {}
        self._unattributed_reads: set[str] = set()
        self._active_test: str | None = None
        self._installed = False
        self._recording = False
        self._errors = 0
        self._error_samples: list[str] = []

    def _record_failure(self, exc: Exception) -> None:
        self._errors += 1
        if len(self._error_samples) < MAX_ERROR_SAMPLES:
            self._error_samples.append(repr(exc))

    def _relative(self, raw: object) -> str | None:
        """Express one event's path argument relative to the repository root.

        Returns None for anything outside the project, inside an excluded
        directory, or not a path at all. ``open(fd)`` passes an integer, and a
        file descriptor names no path; a bytes path is dropped for the same
        reason, since the profile's paths are the str ones git reports.

        Not a recording failure in any of those cases: they are events this map
        has nothing to say about, whereas the error count means a read that
        should have been recorded was lost.
        """
        if not isinstance(raw, (str, os.PathLike)):
            return None
        path = os.fspath(raw)
        if not isinstance(path, str):
            return None
        # The overwhelming majority of a suite's opens are absolute paths into
        # the standard library and site-packages. Rejecting those on a string
        # comparison keeps the hot path free of the path handling below, which
        # allocates and asks the OS for the working directory. The prefix
        # carries no trailing separator, so the root itself passes -- a test
        # that lists the whole repository is recorded like any other listing --
        # and a sibling directory that merely shares the prefix is rejected by
        # relative_to below.
        if path.startswith(os.sep) and not path.startswith(self._root_prefix):
            return None

        absolute = Path(os.path.normpath(Path(path) if path.startswith(os.sep) else Path.cwd() / path))
        try:
            relative = absolute.relative_to(self._root)
        except ValueError:
            return None
        if EXCLUDED_PATH_PARTS.intersection(relative.parts):
            return None
        return relative.as_posix() if relative.parts else WHOLE_REPOSITORY

    def _record_read(self, args: Sequence[Any]) -> None:
        path, mode, flags = args[0], args[1], args[2]
        if not is_read_mode(mode, flags):
            return
        relative = self._relative(path)
        if relative is None or relative.endswith(PYTHON_SUFFIX):
            return
        if self._active_test is None:
            self._unattributed_reads.add(relative)
        else:
            self._reads.setdefault(self._active_test, set()).add(relative)

    def _record_listing(self, args: Sequence[Any]) -> None:
        # Discarded outside a test: see this module's docstring.
        if self._active_test is None:
            return
        relative = self._relative(args[0])
        if relative is None:
            return
        self._listings.setdefault(self._active_test, set()).add(relative)

    def audit(self, event: str, args: Sequence[Any]) -> None:
        """The audit hook itself, called for every audited event in the process.

        Ordered so the overwhelming majority of events -- everything that is
        neither an open nor a listing -- cost one string comparison and return.
        """
        if not self._recording:
            return
        try:
            if event == OPEN_EVENT:
                self._record_read(args)
            elif event in LISTING_EVENTS:
                self._record_listing(args)
        # A blind catch is deliberate and load-bearing, as in the import tracer:
        # this runs inside every file operation of the suite being profiled, so
        # letting anything escape would take down the user's whole test run. The
        # count is surfaced in the profile because a swallowed failure is a
        # missing read, and a missing read under-selects.
        except Exception as exc:  # noqa: BLE001
            self._record_failure(exc)

    def install(self) -> None:
        """Start recording. Safe to call more than once.

        The hook is added at most once, because ``sys.addaudithook`` cannot be
        undone: a second one would double every event this tracer sees.
        """
        if not self._installed:
            sys.addaudithook(self.audit)
            self._installed = True
        self._recording = True

    def uninstall(self) -> None:
        """Stop recording. The hook itself stays installed, as it must."""
        self._recording = False

    def set_active_test(self, test_id: str | None) -> None:
        """Attribute everything recorded from now on to ``test_id``, or to nothing."""
        self._active_test = test_id

    def snapshot(self) -> ReadMap:
        """Everything recorded so far, as the profile stores it."""
        return ReadMap(
            reads_by_test={test: frozenset(paths) for test, paths in self._reads.items()},
            listings_by_test={test: frozenset(paths) for test, paths in self._listings.items()},
            unattributed_reads=frozenset(self._unattributed_reads),
            recording_errors=self._errors,
            error_samples=tuple(self._error_samples),
        )


class ReadMapIngestError(RuntimeError):
    """Raised when a read map written by the profiling hook cannot be read."""


def write_read_map(read_map: ReadMap, path: Path) -> None:
    """Write one process's captured read map, in a stable order."""
    path.write_text(ReadMapModel.from_read_map(read_map).model_dump_json())


def read_read_map(path: Path) -> ReadMap:
    """Read back one process's captured read map."""
    try:
        return ReadMapModel.model_validate_json(path.read_bytes()).to_read_map()
    except (OSError, ValidationError) as exc:
        msg = (
            f"could not read the file read map written by the profiling hook ({path}): {exc}. "
            "A partial map would look exactly like a suite whose tests read fewer files than "
            "they do, which under-selects tests rather than failing."
        )
        raise ReadMapIngestError(msg) from exc


def merge_read_maps(maps: Iterable[ReadMap]) -> ReadMap:
    """Combine the maps several processes captured of the same run.

    Under pytest-xdist each worker sees only the reads its own slice of the
    suite provoked, so the run's map is the union of theirs. A file one worker
    could only read outside a test may well have been read inside one by
    another, so the unattributed set is resolved against the merged
    attributions rather than simply unioned -- as ``merge_graphs`` already does
    for modules nothing was seen to import.
    """
    reads: dict[str, set[str]] = {}
    listings: dict[str, set[str]] = {}
    unattributed: set[str] = set()
    errors = 0
    samples: list[str] = []

    for read_map in maps:
        for test, paths in read_map.reads_by_test.items():
            reads.setdefault(test, set()).update(paths)
        for test, directories in read_map.listings_by_test.items():
            listings.setdefault(test, set()).update(directories)
        unattributed |= read_map.unattributed_reads
        errors += read_map.recording_errors
        samples.extend(read_map.error_samples)

    attributed = set().union(*reads.values()) if reads else set()

    return ReadMap(
        reads_by_test={test: frozenset(paths) for test, paths in reads.items()},
        listings_by_test={test: frozenset(directories) for test, directories in listings.items()},
        unattributed_reads=frozenset(unattributed - attributed),
        recording_errors=errors,
        error_samples=tuple(samples[:MAX_ERROR_SAMPLES]),
    )
