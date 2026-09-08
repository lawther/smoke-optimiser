"""Invert a profile into the maps downwind selection reads from.

``ProfilingData`` stores the relation test -> files: each ``ProfilingOutcome``
lists the files that one test executed. Downwind selection needs the opposite
direction -- given a changed file, which tests does it reach -- and needs it
without rescanning every test record for every changed file in a commit. This
module builds that inversion once, from a loaded profile, as a pure function
with no IO and no git.

Six relations come out of a profile. Three of them answer for Python:

* file -> tests that executed it, inverted from ``files_covered``. A
  branchless file (constants, re-exports, model declarations) still executed
  under a test's context and belongs here even though it contributes no
  branch ids.
* file -> files that transitively import it, inverted from
  ``ImportGraph.edges`` and closed over however many hops. This is the only
  relation that can answer for a file that only ever runs at import time,
  which coverage records under no test context at all.
* file -> tests DEFINED in it, inverted from the node ids ``ProfilingData``
  is keyed by. A dependent is a file and the answer is node ids, so
  something has to cross that gap, and coverage cannot: whether a test
  module appears in any ``files_covered`` depends entirely on the ``--cov``
  target the run happened to use. Under ``--cov=mypackage`` no test executed
  a test module, so the file -> tests relation is empty for every one of
  them, and a changed import-time-only module would select nothing at all.
  The node ids carry the relation regardless.

Three more answer for everything else, which the Python relations cannot see
at all:

* file -> tests that READ it, inverted from ``files_read``. The only relation
  a data file has to a test, since coverage and the import graph both describe
  Python execution and a YAML fixture takes part in neither.
* directory -> tests that LISTED it, inverted from ``directories_listed``. A
  test that globs a directory never names the file it opens, so this is what
  answers for a file added since the profile was taken.
* directory -> tests that read a file directly in it, regrouped from
  ``files_read``. A file the profile never saw has no measurement of its own
  and inherits what was measured about the directory holding it -- and a
  directory nothing ever read is a measured fact in its own right, which is
  what lets a new file under ``docs/`` select nothing rather than everything.

The last of those is DERIVED rather than stored, exactly as the import closure
is. It is a regrouping of ``files_read``, and a second copy of it in the
profile could only ever disagree with the first.

The read relations are SPARSE where the Python ones are dense. A data file is
not a file the profile "knows" in :meth:`DownwindMaps.knows`'s sense, so there
is no key set to fill and no ambiguity to protect against: an empty answer is
answered with the empty set rather than an error. Whether that emptiness is
MEASURED -- the file was there and nothing opened it -- is a separate question,
which :meth:`DownwindMaps.was_present` answers from the profile's denominator.

The three Python maps are dense: every file the profile knows about, from
``DownwindMaps.knows``, has an entry in each, empty where it has no tests,
no dependents or no tests defined in it. Density means a lookup miss is never ambiguous
between "known, but empty" and "never seen" -- the caller need not
cross-reference a third set to tell them apart. Measured at the largest
observed repo scale (271 files), that costs a few tens of kilobytes and a
few microseconds of build time, once per profile.

Facts about each recorder's own trustworthiness ride along with the maps:
which files the import graph could not attribute an importer to, how many
edges it failed to record, which files were read outside any test, and how
many reads went missing. None is a relation, but each qualifies the answers
the relations give, and the rules that read them read nothing else -- so they
live here rather than making the caller carry the raw profile alongside the
maps and keep the two in step.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from smoke_optimiser.profiler.scope import WHOLE_REPOSITORY, under_root

if TYPE_CHECKING:
    from collections.abc import Mapping

    from smoke_optimiser.profiler.models import ProfilingData


class UnknownFileError(KeyError):
    """A downwind query named a file the maps have never seen.

    Raised rather than answered with an empty result, because an empty
    dependents or tests set for a KNOWN file is a real, meaningful answer --
    conflating it with "never seen" is exactly the silent under-selection
    this map exists to prevent. Callers must gate on ``knows()`` first.
    """


def _known_files(profile: ProfilingData) -> frozenset[str]:
    """Every file the profile knows about at all.

    The union of everything coverage measured, whether or not any test
    executed it, every file the import graph mentions -- as an importer, as
    something imported, or as a module loaded but unattributable to any
    importer (test modules, conftest.py, plugins pytest loaded before the
    tracer was installed) -- and every file a recorded test is defined in.

    The last of those is rarely news, since a test module reaches the graph
    as an importer of whatever it imports. It matters when a test module
    imports nothing measured, and it keeps the node id map from being the
    one relation whose keys the maps deny knowing.
    """
    graph = profile.import_graph
    graph_files = {edge.importer for edge in graph.edges} | {edge.imported for edge in graph.edges}
    defining_files = {_defining_file(test_id) for test_id in profile.tests}
    return profile.measured_files | graph_files | graph.unattributed_modules | defining_files


def _defining_file(test_id: str) -> str:
    """The file a pytest node id names, which is everything before the first ``::``.

    A node id is ``path::class::test[param]``, and a parametrised id can
    carry a further ``::`` inside its brackets, so only the first separator
    delimits the path.
    """
    return test_id.split("::", maxsplit=1)[0]


def _tests_in_module_by_file(profile: ProfilingData, known_files: frozenset[str]) -> dict[str, frozenset[str]]:
    """Invert the profile's node ids into file -> the tests DEFINED in it."""
    defined: dict[str, set[str]] = {}
    for test_id in profile.tests:
        defined.setdefault(_defining_file(test_id), set()).add(test_id)
    return {path: frozenset(defined.get(path, ())) for path in known_files}


def _tests_by_file(profile: ProfilingData, known_files: frozenset[str]) -> dict[str, frozenset[str]]:
    """Invert ``files_covered`` into file -> the tests that executed it."""
    covering: dict[str, set[str]] = {}
    for outcome in profile.tests.values():
        for path in outcome.files_covered:
            covering.setdefault(path, set()).add(outcome.test_id)
    return {path: frozenset(covering.get(path, ())) for path in known_files}


def _parent_directory(path: str) -> str:
    """The directory holding ``path``, as the read map spells directories.

    A file at the top level of the repository is held by
    :data:`WHOLE_REPOSITORY`, the same name the tracer records when a test
    lists the root, so one spelling answers for both.
    """
    directory, separator, _ = path.rpartition("/")
    return directory if separator else WHOLE_REPOSITORY


def _tests_reading_file(profile: ProfilingData) -> dict[str, frozenset[str]]:
    """Invert ``files_read`` into file -> the tests that opened it.

    Sparse, unlike the Python maps: the keys are data files, which are not
    files the profile "knows" in the :meth:`DownwindMaps.knows` sense, so
    there is no dense key set to fill and a miss is answered with the empty
    set rather than an error.
    """
    reading: dict[str, set[str]] = {}
    for outcome in profile.tests.values():
        for path in outcome.files_read:
            reading.setdefault(path, set()).add(outcome.test_id)
    return {path: frozenset(tests) for path, tests in reading.items()}


def _tests_listing_directory(profile: ProfilingData) -> dict[str, frozenset[str]]:
    """Invert ``directories_listed`` into directory -> the tests that listed it."""
    listing: dict[str, set[str]] = {}
    for outcome in profile.tests.values():
        for directory in outcome.directories_listed:
            listing.setdefault(directory, set()).add(outcome.test_id)
    return {directory: frozenset(tests) for directory, tests in listing.items()}


def _tests_reading_in_directory(profile: ProfilingData) -> dict[str, frozenset[str]]:
    """Directory -> every test that read a file directly in it.

    This is what answers for a file the profile never saw. A file added after
    the run has no measurement of its own, so it inherits what was measured
    about the directory holding it -- and a directory nothing ever read is a
    measured fact in its own right, which is what lets a new doc under docs/
    select nothing rather than everything.

    Derived here rather than stored, exactly as the dependents closure is: it
    is a regrouping of ``files_read``, and a second copy in the profile could
    only ever disagree with the first.
    """
    reading: dict[str, set[str]] = {}
    for outcome in profile.tests.values():
        for path in outcome.files_read:
            reading.setdefault(_parent_directory(path), set()).add(outcome.test_id)
    return {directory: frozenset(tests) for directory, tests in reading.items()}


def _direct_dependents(profile: ProfilingData) -> dict[str, set[str]]:
    """One hop only: file -> the files that directly import it."""
    dependents: dict[str, set[str]] = {}
    for edge in profile.import_graph.edges:
        dependents.setdefault(edge.imported, set()).add(edge.importer)
    return dependents


def _closure(start: str, direct_dependents: dict[str, set[str]]) -> frozenset[str]:
    """Every file reachable from ``start`` by one or more import hops.

    A plain visited-set walk, not memoised per node: an import cycle simply
    means the walk revisits a node already on the visited set and stops
    there, which is what makes a cycle terminate. It also makes a cycle
    self-inclusive on both sides for free -- A -> B -> A visits B from A,
    then revisits A from B, so A ends up in its own closure, and the same
    walk starting from B reaches the identical fixed point. Memoising
    partial results keyed by node, by contrast, can cache a closure computed
    mid-cycle before the cycle has been walked all the way round, permanently
    losing a member -- so each start file gets its own fresh walk.
    """
    visited: set[str] = set()
    stack = list(direct_dependents.get(start, ()))
    while stack:
        node = stack.pop()
        if node in visited:
            continue
        visited.add(node)
        stack.extend(direct_dependents.get(node, ()))
    return frozenset(visited)


def _dependents_by_file(profile: ProfilingData, known_files: frozenset[str]) -> dict[str, frozenset[str]]:
    """Invert ``ImportGraph.edges`` into file -> its transitive dependents."""
    direct = _direct_dependents(profile)
    return {path: _closure(path, direct) for path in known_files}


@dataclass(frozen=True)
class DownwindMaps:
    """The inverted profile: file -> tests, file -> dependents, file -> its own tests.

    Built by :meth:`from_profile`; query with :meth:`knows`,
    :meth:`tests_executing`, :meth:`dependents_of`,
    :meth:`tests_in_module` and :meth:`tests_at_or_below`. so-jr5.2's rules are
    the only intended caller -- this class answers "what does the profile
    say", never "should this file force a full run". :meth:`is_unattributed`
    and :attr:`resolution_errors` keep to that split: they report where the
    import graph is blind, and leave what to do about it to the rules.
    """

    _tests_by_file: Mapping[str, frozenset[str]]
    _dependents_by_file: Mapping[str, frozenset[str]]
    _tests_in_module_by_file: Mapping[str, frozenset[str]]
    _known_files: frozenset[str]
    _unattributed_modules: frozenset[str]
    _resolution_errors: int
    _tests_reading_file: Mapping[str, frozenset[str]]
    _tests_listing_directory: Mapping[str, frozenset[str]]
    _tests_reading_in_directory: Mapping[str, frozenset[str]]
    _unattributed_reads: frozenset[str]
    _present_files: frozenset[str]
    _read_errors: int

    @classmethod
    def from_profile(cls, profile: ProfilingData) -> DownwindMaps:
        """Build every map from a loaded profile, once."""
        known_files = _known_files(profile)
        graph = profile.import_graph
        return cls(
            _tests_by_file=_tests_by_file(profile, known_files),
            _dependents_by_file=_dependents_by_file(profile, known_files),
            _tests_in_module_by_file=_tests_in_module_by_file(profile, known_files),
            _known_files=known_files,
            _unattributed_modules=graph.unattributed_modules,
            _resolution_errors=graph.resolution_errors,
            _tests_reading_file=_tests_reading_file(profile),
            _tests_listing_directory=_tests_listing_directory(profile),
            _tests_reading_in_directory=_tests_reading_in_directory(profile),
            _unattributed_reads=profile.reads.unattributed_reads,
            _present_files=profile.present_files,
            _read_errors=profile.reads.recording_errors,
        )

    def knows(self, path: str) -> bool:
        """Does the profile have any entry at all for ``path``?"""
        return path in self._known_files

    def tests_executing(self, path: str) -> frozenset[str]:
        """Tests whose ``files_covered`` included ``path``.

        Raises :class:`UnknownFileError` if ``knows(path)`` is false.
        """
        if path not in self._known_files:
            raise UnknownFileError(path)
        return self._tests_by_file[path]

    def dependents_of(self, path: str) -> frozenset[str]:
        """Every file that transitively imports ``path``, however many hops away.

        Raises :class:`UnknownFileError` if ``knows(path)`` is false.
        """
        if path not in self._known_files:
            raise UnknownFileError(path)
        return self._dependents_by_file[path]

    def tests_in_module(self, path: str) -> frozenset[str]:
        """Tests DEFINED in ``path``, read off the node ids the profile is keyed by.

        Empty for every file that is not a test module, and for a test
        module whose tests the profiled run never recorded. Unlike
        :meth:`tests_executing` this does not depend on what the run's
        ``--cov`` target measured, which is what makes it able to answer for
        a test module coverage never saw.

        Raises :class:`UnknownFileError` if ``knows(path)`` is false.
        """
        if path not in self._known_files:
            raise UnknownFileError(path)
        return self._tests_in_module_by_file[path]

    def tests_at_or_below(self, directory: str) -> frozenset[str]:
        """Every test DEFINED in a file at or below ``directory``.

        pytest's own scoping rule, asked of the profile: a conftest.py applies
        to every test collected at or below the directory containing it, and a
        node id's path is the file its test is defined in. Read off the node
        ids rather than off coverage, so the answer holds under a ``--cov``
        target that never measured the test tree -- which is the only
        configuration that needs to ask.

        ``directory`` is a repository-relative posix path, or
        ``WHOLE_REPOSITORY`` for the root, where the answer is every test the
        profile recorded. No :class:`UnknownFileError`: a directory is not a
        file the maps have an entry for, and one holding no test the profile
        saw is genuinely answered by the empty set.
        """
        return frozenset(
            test_id
            for path, tests in self._tests_in_module_by_file.items()
            if under_root(path, directory)
            for test_id in tests
        )

    def is_unattributed(self, path: str) -> bool:
        """Was ``path`` loaded without the tracer seeing anything import it?

        True means :meth:`dependents_of` cannot answer for this file: an empty
        closure is the graph being blind, not the file being a leaf. Test
        modules and conftest files are the legitimate case, and :meth:`knows`
        deliberately folds them in so they count as known -- which is exactly
        why telling the two apart needs its own query.

        Raises :class:`UnknownFileError` if ``knows(path)`` is false, for the
        same reason the map lookups do: answering False for a file never seen
        would conflate it with a file the graph did attribute.
        """
        if path not in self._known_files:
            raise UnknownFileError(path)
        return path in self._unattributed_modules

    def tests_reading(self, path: str) -> frozenset[str]:
        """Tests whose ``files_read`` included ``path``.

        No :class:`UnknownFileError`: a data file is not a file the maps
        "know" in the Python sense, and the empty set is a real answer for one
        the tracer watched and no test opened. Whether that emptiness is
        MEASURED or merely unobserved is :meth:`was_present`'s question, not
        this one's.
        """
        return self._tests_reading_file.get(path, frozenset())

    def tests_listing(self, directory: str) -> frozenset[str]:
        """Tests that listed or globbed ``directory``.

        The relation that answers for a file a test never names: a glob
        discovers whatever is in the directory at the time, including files
        added since the profile was taken.
        """
        return self._tests_listing_directory.get(directory, frozenset())

    def tests_reading_in(self, directory: str) -> frozenset[str]:
        """Tests that read any file directly in ``directory``.

        Empty means nothing the profile saw ever touched this directory, which
        is what makes a new file in it answerable with "nothing" rather than
        with the full suite.
        """
        return self._tests_reading_in_directory.get(directory, frozenset())

    def was_present(self, path: str) -> bool:
        """Did this file exist when the profile was taken?

        The read map's denominator. Without it, "no test read this file" and
        "this file was not there to be read" are the same observation, and
        only the first of them can warrant selecting nothing.
        """
        return path in self._present_files

    def is_unattributed_read(self, path: str) -> bool:
        """Was ``path`` read outside any test -- at import or collection time?

        True means the read map cannot answer for it: something depends on the
        file, and no test can be named as the dependant.
        """
        return path in self._unattributed_reads

    @property
    def read_errors(self) -> int:
        """How many reads the tracer failed to record and swallowed.

        Each one is a MISSING read of unknown identity, so any non-zero count
        makes every answer drawn from the read map a possible under-estimate.
        """
        return self._read_errors

    @property
    def resolution_errors(self) -> int:
        """How many import edges the tracer failed to record and swallowed.

        Each one is a MISSING edge of unknown identity, so any non-zero count
        makes every closure :meth:`dependents_of` returns a possible
        under-estimate. The count, not merely the fact, so a refusal can say
        how incomplete the graph is.
        """
        return self._resolution_errors
