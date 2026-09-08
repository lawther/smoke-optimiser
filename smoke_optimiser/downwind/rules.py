"""Answer from the downwind maps, or refuse and name the blind spot.

Given the changed set and the maps, this produces either the tests downwind
of that change or a refusal naming the inputs it could not answer for.
Everything else in the downwind line of work feeds this or carries its
answer.

It is one component because the rules are not independent. A changed file
can be answerable by coverage and unanswerable by the graph at the same
time; a conftest.py is both a Python file with a directory rule and a module
the graph may have edges for; a degraded graph refuses regardless of what
changed. Applied as separate filters, one rule quietly wins over another.

DOWNWIND, for a changed Python file the maps know, is the union of the tests
that executed it and the tests whose modules transitively import it. The
maps answer the second half with FILES, so each dependent is turned into
tests by both available relations:

* the tests DEFINED in it, which is immune to what the profiling run's
  ``--cov`` measured, and so is the only half that can answer under a
  profile that never measured the test modules;
* the tests that EXECUTED it, which is the only half that sees
  fixture-mediated reach -- a conftest.py imports a changed module, no test
  module imports the conftest, so the import closure stops there while
  coverage recorded every test that used its fixtures.

Each half covers the other's blind spot, so the answer is their union --
except where BOTH are empty for a file the walk cannot continue past. A
conftest.py defines no test, and under a ``--cov`` that measured only the
package it is in no ``files_covered`` either, so a changed module whose only
route to the suite is a conftest import reaches a file that turns into no
node ids at all. That is a DEAD END: the walk stopped because nothing
imports the file, not because the file leads nowhere. A dead end at a
conftest.py falls back to pytest's own scoping rule, every test at or below
its directory; a dead end anywhere else -- a pytest11 plugin belonging to
the project, a module loaded through machinery the tracer cannot follow --
has no such rule to fall back on and is a blind spot.

A changed NON-PYTHON path is answered from the read map instead, by a ladder
of its own that :func:`_read_answer` documents. That path used to be an
unconditional refusal, and it is the one place in this module where absence is
allowed to count as evidence: a file the profiling run measured as unread
selects nothing rather than everything. :mod:`smoke_optimiser.downwind.blind_spots`
states what warrants that and what bounds it.

REFUSALS ACCUMULATE. Every offending input contributes its own blind spot,
so a developer sees everything they would have to fix to get a subset again
rather than fixing one cause, re-running, and meeting the next. The outcome
is the same full suite either way; what differs is whether they can act on
it once.

The rules are pure and IO-free: nothing here touches the filesystem or git,
and nothing asks where the changed set or the existing files came from.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, NamedTuple

from smoke_optimiser.downwind.blind_spots import BlindSpot, BlindSpotReason
from smoke_optimiser.downwind.environment_files import is_environment_file
from smoke_optimiser.profiler.scope import CONFTEST, WHOLE_REPOSITORY, parent_directory

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from smoke_optimiser.downwind.changes import ChangedFile
    from smoke_optimiser.downwind.maps import DownwindMaps

PYTHON_SUFFIX = ".py"
"""What the maps are a map OF, so anything else is outside what they can say."""


@dataclass(frozen=True)
class DownwindSelection:
    """The tests downwind of the change, as node ids.

    Empty is a real answer, not a refusal: an empty changed set has nothing
    downwind of it, and a changed file no test reaches genuinely selects
    nothing.
    """

    node_ids: frozenset[str]


@dataclass(frozen=True)
class DownwindRefusal:
    """The rules could not answer, and these are the inputs they could not answer for.

    Deliberately carries no node ids. A refusal means the full suite, and a
    caller that cannot read a test list off it cannot mistake "I could not
    answer, so here is everything" for "these 37 tests are the answer".

    A set, because the rules that produce these are independent and no
    reason ranks above another; :func:`in_report_order` fixes the order
    wherever a refusal is written out or printed.
    """

    blind_spots: frozenset[BlindSpot]

    def __post_init__(self) -> None:
        if not self.blind_spots:
            message = "a refusal must name at least one blind spot"
            raise ValueError(message)


DownwindAnswer = DownwindSelection | DownwindRefusal
"""What :func:`downwind_of` returns: a selection or a refusal, never both."""


def _is_conftest(path: str) -> bool:
    return path.rsplit("/", maxsplit=1)[-1] == CONFTEST


def _blind_spot_reason(maps: DownwindMaps, path: str) -> BlindSpotReason | None:
    """Which rule refuses for this changed path, if any. First match wins.

    Only Python paths reach here. A non-Python path is answered from the read
    map by :func:`_read_answer` instead, which has refusals of its own.

    The order is explicit rather than an accident of evaluation, because
    several rules match the same path and the reason is as much the product
    as the refusal is:

    * unknown-path before everything that queries the maps, all of which
      raise :class:`UnknownFileError` for a path they have never seen;
    * conftest before unattributed, since a changed conftest.py is almost
      always unattributed as well, and so-017 replaces the conftest reason
      rather than that one.
    """
    if not maps.knows(path):
        return BlindSpotReason.UNKNOWN_PATH
    if _is_conftest(path):
        return BlindSpotReason.CHANGED_CONFTEST
    if maps.is_unattributed(path) and not maps.tests_in_module(path):
        # A file with tests defined in it is unattributed BECAUSE pytest
        # loaded it, so its empty closure is the truth rather than the graph
        # being blind, and it answers with its own tests. Anything else
        # unattributed -- a plugin, a module reached through machinery the
        # tracer cannot follow -- really is unanswerable. A test module
        # something does import carries an edge and is not unattributed at
        # all, so it never reaches this rule.
        return BlindSpotReason.UNATTRIBUTED_IMPORT
    return None


def _conftest_directory(path: str) -> str:
    """The directory a conftest.py governs, as pytest scopes it.

    A conftest at the repository root governs everything, which is
    :data:`WHOLE_REPOSITORY` rather than the empty string so that the one
    "at or below" predicate in :mod:`smoke_optimiser.profiler.scope` answers
    for it like any other root.
    """
    directory, separator, _ = path.rpartition("/")
    return directory if separator else WHOLE_REPOSITORY


class _FileAnswer(NamedTuple):
    """What one answerable changed file contributed: tests, and its dead ends.

    Both, because walking the closure is where a dead end is discovered, and
    a file can reach answerable tests down one branch while another branch
    stops at something unanswerable. Returning only the tests would drop the
    dead end silently, which is precisely the failure this rule exists to
    catch.
    """

    node_ids: frozenset[str]
    blind_spots: frozenset[BlindSpot]


def _tests_downwind_of(maps: DownwindMaps, path: str) -> _FileAnswer:
    """Every test the maps put downwind of one answerable changed file.

    A reached file that yields no tests by either relation is only a problem
    when the walk cannot continue past it either. An ordinary module in the
    middle of the closure yields nothing and is still fine: something
    imports it, so its own importers are already in the closure and will be
    asked in their turn. Only an UNATTRIBUTED file is terminal -- nothing was
    seen to import it, so there is nowhere further to walk -- and an
    unattributed file that also yields no tests is a route to the suite that
    ends in nothing.
    """
    selected: set[str] = set()
    dead_ends: set[BlindSpot] = set()

    for reached in maps.dependents_of(path) | {path}:
        tests = maps.tests_in_module(reached) | maps.tests_executing(reached)
        if tests:
            selected |= tests
        elif not maps.is_unattributed(reached):
            continue
        elif _is_conftest(reached):
            # so-017's rule, applied to a conftest that merely sits on the
            # path rather than one that changed: pytest applies a conftest to
            # every test collected at or below its directory, and that is
            # answerable from the node ids whatever --cov measured.
            selected |= maps.tests_at_or_below(_conftest_directory(reached))
        else:
            dead_ends.add(BlindSpot(reason=BlindSpotReason.TERMINAL_DEAD_END, file=reached))

    return _FileAnswer(node_ids=frozenset(selected), blind_spots=frozenset(dead_ends))


def _read_answer(maps: DownwindMaps, path: str, environment_files: Sequence[str]) -> _FileAnswer:
    """What the read map says about one changed non-Python path.

    Four rules, in this order, and the order is what makes the map useful
    rather than merely safe:

    1. An ENVIRONMENT-DEFINING path refuses outright. Nothing opens a lockfile
       while the suite runs, so the map would answer "nothing depends on this"
       for a file that changes the behaviour of every test.
    2. A path read OUTSIDE any test refuses. Something depends on it and no
       test can be named as the dependant, so the map cannot answer.
    3. A path PRESENT when the profile was taken is answered by the tests that
       read it, plus the tests that listed its directory. Empty is a real
       answer here, and the only place in this module where absence is
       evidence: the tracer watched every open for the whole suite, and the
       file was there to be opened. The listers are unioned in because no rule
       reads ``ChangeKind``, and DELETING a file changes what a listing returns
       even when nothing ever opened it.
    4. A NEW path has no measurement of its own, so it inherits what was
       measured about the directory holding it: the tests that list that
       directory, which is how a glob discovers a file nobody names, and the
       tests that read its siblings. A directory nothing ever read or listed
       yields nothing -- a new file under docs/ selects nothing, exactly as a
       changed one does -- and that is a measured fact about the directory, not
       an assumption about the file.

    Returns a :class:`_FileAnswer` like the Python walk does, so both kinds of
    changed path contribute tests and refusals through the same channel.
    """
    if is_environment_file(path, environment_files):
        return _FileAnswer(
            node_ids=frozenset(),
            blind_spots=frozenset({BlindSpot(reason=BlindSpotReason.ENVIRONMENT_FILE, file=path)}),
        )
    if maps.is_unattributed_read(path):
        return _FileAnswer(
            node_ids=frozenset(),
            blind_spots=frozenset({BlindSpot(reason=BlindSpotReason.UNATTRIBUTED_READ, file=path)}),
        )

    directory = parent_directory(path)
    if maps.was_present(path):
        selected = maps.tests_reading(path) | maps.tests_listing(directory)
    else:
        selected = maps.tests_listing(directory) | maps.tests_reading_in(directory)
    return _FileAnswer(node_ids=selected, blind_spots=frozenset())


def _expiry_blind_spots(maps: DownwindMaps, existing_files: Iterable[str]) -> set[BlindSpot]:
    """One blind spot per file that exists now and the maps have never seen.

    From that file's arrival the maps can answer confidently and wrongly:
    whatever it contains is absent from every answer they give.
    """
    return {
        BlindSpot(reason=BlindSpotReason.EXPIRED_PROFILE, file=path) for path in existing_files if not maps.knows(path)
    }


def downwind_of(
    maps: DownwindMaps,
    changed_files: frozenset[ChangedFile],
    existing_files: frozenset[str],
    environment_files: Sequence[str],
) -> DownwindAnswer:
    """The tests downwind of ``changed_files``, or a refusal naming the blind spots.

    Args:
        maps: The inverted profile. Every fact these rules use comes from
            here, including how much the import graph can be trusted.
        changed_files: The union of staged, unstaged and untracked changes,
            with renames already resolved to the old path. No rule reads
            ``ChangeKind``: a deletion is looked up exactly like any other
            change, since the tests that executed a file are the ones a
            deletion is most likely to break.
        existing_files: The tracked files that exist in the working tree now,
            already narrowed to the scope the profile was captured under. A
            file here that the maps do not know is what expires the profile.
        environment_files: Patterns naming paths that define the environment
            the whole suite runs in, which refuse regardless of what any map
            says about them.

    A path that is both a changed file the maps have never seen and a file
    that expires the profile reports BOTH reasons. Each is independently
    true, neither is derived from the other, and suppressing one would be the
    silent behaviour; what matters is that the reasons are defined by the
    rule order above rather than by which rule happened to run first.
    """
    blind_spots: set[BlindSpot] = set()
    node_ids: set[str] = set()

    # Paths rather than ChangedFile: no rule reads the kind, and taking the
    # set of them deduplicates a path git reported under two kinds at once.
    changed_paths = {changed.path for changed in changed_files}
    for path in changed_paths:
        if not path.endswith(PYTHON_SUFFIX):
            # Answered from the read map rather than refused. This is the one
            # kind of changed path the Python relations were never able to say
            # anything about at all.
            answer = _read_answer(maps, path, environment_files)
            node_ids |= answer.node_ids
            blind_spots |= answer.blind_spots
            continue

        reason = _blind_spot_reason(maps, path)
        if reason is None:
            answer = _tests_downwind_of(maps, path)
            node_ids |= answer.node_ids
            blind_spots |= answer.blind_spots
        else:
            blind_spots.add(BlindSpot(reason=reason, file=path))

    if maps.resolution_errors:
        # Blunt on purpose, and in the safe direction: each swallowed failure
        # is a missing edge of unknown identity, so the count alone cannot say
        # which closure it shortened. so-n6b.2 localises the doubt to the
        # importer; until then the whole graph is untrustworthy.
        blind_spots.add(BlindSpot(reason=BlindSpotReason.RESOLUTION_ERRORS, resolution_errors=maps.resolution_errors))

    if maps.read_errors and any(not path.endswith(PYTHON_SUFFIX) for path in changed_paths):
        # Gated on a non-Python path having actually changed, unlike the import
        # graph's equivalent above. A lost read can only shorten an answer drawn
        # from the read map, and no Python path draws one -- so refusing for a
        # pure-Python commit would spend a full suite on doubt that cannot apply
        # to anything in it.
        blind_spots.add(BlindSpot(reason=BlindSpotReason.READ_ERRORS, read_errors=maps.read_errors))

    blind_spots |= _expiry_blind_spots(maps, existing_files)

    if blind_spots:
        return DownwindRefusal(blind_spots=frozenset(blind_spots))
    return DownwindSelection(node_ids=frozenset(node_ids))
