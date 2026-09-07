"""The vocabulary for an input the downwind maps cannot answer for.

A blind spot means the full suite, with the input that caused it named. The
epic requires the concept to keep this name in every layer -- the maps, the
rules, the selection file, and the console output the developer reads when
the tool declines to select -- so it lives in neither the rules that produce
it nor the schema that serialises it, and both import it from here.

Every reason over-selects rather than under-selects, so seeing one never
means a test was silently dropped.
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
    NON_PYTHON_FILE = "non_python_file"
    UNATTRIBUTED_IMPORT = "unattributed_import"
    EXPIRED_PROFILE = "expired_profile"
    CHANGED_CONFTEST = "changed_conftest"
    RESOLUTION_ERRORS = "resolution_errors"


@dataclass(frozen=True)
class BlindSpot:
    """One rule's refusal, naming the input it could not answer for.

    Attributes:
        reason: Which rule refused.
        file: The path it refused for. None only for RESOLUTION_ERRORS, the
            one reason that is a property of the whole import graph rather
            than of any single file.
        resolution_errors: How many edges the tracer failed to record,
            populated only for RESOLUTION_ERRORS, whose refusal is required
            to say how incomplete the graph is.
    """

    reason: BlindSpotReason
    file: str | None = None
    resolution_errors: int | None = None

    def __post_init__(self) -> None:
        is_resolution_errors = self.reason is BlindSpotReason.RESOLUTION_ERRORS
        if (self.file is None) != is_resolution_errors:
            file_message = "file must be set iff reason is not RESOLUTION_ERRORS"
            raise ValueError(file_message)
        if (self.resolution_errors is None) == is_resolution_errors:
            count_message = "resolution_errors must be set iff reason is RESOLUTION_ERRORS"
            raise ValueError(count_message)


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
