"""Invert a profile into the maps downwind selection reads from.

``ProfilingData`` stores the relation test -> files: each ``ProfilingOutcome``
lists the files that one test executed. Downwind selection needs the opposite
direction -- given a changed file, which tests does it reach -- and needs it
without rescanning every test record for every changed file in a commit. This
module builds that inversion once, from a loaded profile, as a pure function
with no IO and no git.

Two relations come out of a profile:

* file -> tests that executed it, inverted from ``files_covered``. A
  branchless file (constants, re-exports, model declarations) still executed
  under a test's context and belongs here even though it contributes no
  branch ids.
* file -> files that transitively import it, inverted from
  ``ImportGraph.edges`` and closed over however many hops. This is the only
  relation that can answer for a file that only ever runs at import time,
  which coverage records under no test context at all.

Both maps are dense: every file the profile knows about, from
``DownwindMaps.knows``, has an entry in both maps, empty where it has no
tests or no dependents. Density means a lookup miss is never ambiguous
between "known, but empty" and "never seen" -- the caller need not
cross-reference a third set to tell them apart. Measured at the largest
observed repo scale (271 files), that costs a few tens of kilobytes and a
few microseconds of build time, once per profile.

Two facts about the import graph's own trustworthiness ride along with the
maps: which files the graph could not attribute an importer to, and how many
edges it failed to record. Neither is a relation, but both qualify the
answers the relations give, and the rules that read them read nothing else --
so they live here rather than making the caller carry the raw profile
alongside the maps and keep the two in step.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

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
    executed it, and every file the import graph mentions -- as an
    importer, as something imported, or as a module loaded but
    unattributable to any importer (test modules, conftest.py, plugins
    pytest loaded before the tracer was installed).
    """
    graph = profile.import_graph
    graph_files = {edge.importer for edge in graph.edges} | {edge.imported for edge in graph.edges}
    return profile.measured_files | graph_files | graph.unattributed_modules


def _tests_by_file(profile: ProfilingData, known_files: frozenset[str]) -> dict[str, frozenset[str]]:
    """Invert ``files_covered`` into file -> the tests that executed it."""
    covering: dict[str, set[str]] = {}
    for outcome in profile.tests.values():
        for path in outcome.files_covered:
            covering.setdefault(path, set()).add(outcome.test_id)
    return {path: frozenset(covering.get(path, ())) for path in known_files}


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
    """The file -> tests and file -> dependents maps, built once per profile.

    Built by :meth:`from_profile`; query with :meth:`knows`,
    :meth:`tests_executing` and :meth:`dependents_of`. so-jr5.2's rules are
    the only intended caller -- this class answers "what does the profile
    say", never "should this file force a full run". :meth:`is_unattributed`
    and :attr:`resolution_errors` keep to that split: they report where the
    import graph is blind, and leave what to do about it to the rules.
    """

    _tests_by_file: Mapping[str, frozenset[str]]
    _dependents_by_file: Mapping[str, frozenset[str]]
    _known_files: frozenset[str]
    _unattributed_modules: frozenset[str]
    _resolution_errors: int

    @classmethod
    def from_profile(cls, profile: ProfilingData) -> DownwindMaps:
        """Build both maps from a loaded profile, once."""
        known_files = _known_files(profile)
        graph = profile.import_graph
        return cls(
            _tests_by_file=_tests_by_file(profile, known_files),
            _dependents_by_file=_dependents_by_file(profile, known_files),
            _known_files=known_files,
            _unattributed_modules=graph.unattributed_modules,
            _resolution_errors=graph.resolution_errors,
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

    @property
    def resolution_errors(self) -> int:
        """How many import edges the tracer failed to record and swallowed.

        Each one is a MISSING edge of unknown identity, so any non-zero count
        makes every closure :meth:`dependents_of` returns a possible
        under-estimate. The count, not merely the fact, so a refusal can say
        how incomplete the graph is.
        """
        return self._resolution_errors
