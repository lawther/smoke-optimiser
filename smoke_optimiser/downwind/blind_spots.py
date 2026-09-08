"""The vocabulary for an input the downwind maps cannot answer for.

A blind spot means the full suite, with the input that caused it named. The
epic requires the concept to keep this name in every layer -- the maps, the
rules, the selection file, and the console output the developer reads when
the tool declines to select -- so it lives in neither the rules that produce
it nor the schema that serialises it, and both import it from here.

Every reason over-selects rather than under-selects, so seeing one never means
a test was silently dropped.

WITH ONE EXCEPTION, which is deliberate and is the reason NON_PYTHON_FILE no
longer exists. A non-Python file the profiling run measured as unread -- it was
present while the suite ran, the audit hook watched every open, and no test
opened it -- selects NOTHING rather than a blind spot. That is absence used as
evidence, which every other rule here refuses to do, and it is warranted by
measurement rather than by assumption: the read map has a denominator, so "no test
read this" is distinguishable from "this was not there to be read". The bound on
it is that a read the tracer cannot see -- a C library calling fopen() -- is
undetectable, so the read map may only ever ADD selections for a Python file and
never justify skipping one.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable


class BlindSpotReason(StrEnum):
    """Why the rules could not answer for one input, forcing the full suite."""

    UNKNOWN_PATH = "unknown_path"
    UNATTRIBUTED_IMPORT = "unattributed_import"
    UNATTRIBUTED_READ = "unattributed_read"
    ENVIRONMENT_FILE = "environment_file"
    EXPIRED_PROFILE = "expired_profile"
    CHANGED_CONFTEST = "changed_conftest"
    TERMINAL_DEAD_END = "terminal_dead_end"
    RESOLUTION_ERRORS = "resolution_errors"
    READ_ERRORS = "read_errors"


WHOLE_MAP_REASONS = frozenset({BlindSpotReason.RESOLUTION_ERRORS, BlindSpotReason.READ_ERRORS})
"""Reasons that are a property of a map rather than of any one changed file.

Both say the same kind of thing: a recorder swallowed a failure, so the map it
built is incomplete by an unknown amount. Neither can name the file it lost,
which is exactly why they carry a count instead."""


@dataclass(frozen=True)
class BlindSpot:
    """One rule's refusal, naming the input it could not answer for.

    Attributes:
        reason: Which rule refused.
        file: The path it refused for. None only for the whole-map reasons,
            which are a property of a recorder rather than of any single file.
        resolution_errors: How many import edges the tracer failed to record,
            populated only for RESOLUTION_ERRORS, whose refusal is required
            to say how incomplete the graph is.
        read_errors: How many file reads the tracer failed to record, on the
            same terms, for READ_ERRORS.
    """

    reason: BlindSpotReason
    file: str | None = None
    resolution_errors: int | None = None
    read_errors: int | None = None

    def __post_init__(self) -> None:
        names_no_file = self.reason in WHOLE_MAP_REASONS
        if (self.file is None) != names_no_file:
            file_message = "file must be set iff the reason is not a whole-map reason"
            raise ValueError(file_message)
        if (self.resolution_errors is None) == (self.reason is BlindSpotReason.RESOLUTION_ERRORS):
            count_message = "resolution_errors must be set iff reason is RESOLUTION_ERRORS"
            raise ValueError(count_message)
        if (self.read_errors is None) == (self.reason is BlindSpotReason.READ_ERRORS):
            read_message = "read_errors must be set iff reason is READ_ERRORS"
            raise ValueError(read_message)


def in_report_order(blind_spots: Iterable[BlindSpot]) -> list[BlindSpot]:
    """The one order blind spots are written and printed in.

    A refusal is held as a set, since the rules that produce it are
    independent and nothing about one reason ranks it above another. But a
    refusal is also read -- in .downwind.json, and on the console -- and an
    order that varies between two identical runs is a diff nobody can
    account for. Sorting happens here, once, rather than at each place that
    renders a refusal.

    By reason, then by file, with the whole-graph reason sorting under the
    empty string because it names no file.
    """
    return sorted(blind_spots, key=lambda spot: (spot.reason.value, spot.file or ""))
