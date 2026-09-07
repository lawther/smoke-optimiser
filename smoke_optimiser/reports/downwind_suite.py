"""Schema for .downwind.json, the file --downwind reads under pytest.

A downwind selection makes a categorical claim -- every test that has ever
executed a changed file, every test whose module transitively imports one,
and every test at or below a conftest.py the import closure terminates at --
never a coverage bet, so it shares nothing with SmokeSuiteFile beyond the
list of node ids. See so-uom.
"""

import json
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, model_validator

from smoke_optimiser.downwind.blind_spots import BlindSpot, BlindSpotReason


class BlindSpotModel(BaseModel):
    """The serialised form of one :class:`BlindSpot`.

    file is None only for RESOLUTION_ERRORS, the one reason that is a
    property of the whole import graph rather than of any single changed
    file. resolution_errors is populated only for that same reason, carrying
    the count jr5.2's refusal is required to name. The invariant is validated
    here as well as on the dataclass, because a file on disk is outside data
    however it was written.
    """

    reason: BlindSpotReason
    file: str | None = None
    resolution_errors: int | None = None

    @classmethod
    def from_blind_spot(cls, blind_spot: BlindSpot) -> "BlindSpotModel":
        """Build the model that writes one rule's refusal to the selection file."""
        return cls(
            reason=blind_spot.reason,
            file=blind_spot.file,
            resolution_errors=blind_spot.resolution_errors,
        )

    @model_validator(mode="after")
    def _check_reason_matches_fields(self) -> "BlindSpotModel":
        is_resolution_errors = self.reason == BlindSpotReason.RESOLUTION_ERRORS
        if (self.file is None) != is_resolution_errors:
            file_message = "file must be set iff reason is not RESOLUTION_ERRORS"
            raise ValueError(file_message)
        if (self.resolution_errors is None) == is_resolution_errors:
            resolution_errors_message = "resolution_errors must be set iff reason is RESOLUTION_ERRORS"
            raise ValueError(resolution_errors_message)
        return self


class ProfileIdentityModel(BaseModel):
    """Identifies the profiling run a downwind selection was computed against.

    commit and timestamp are the pair ProfilingMeta already treats as a run's
    identity: environment fields like python_version or machine describe the
    run rather than distinguishing it from another one taken at the same
    commit.
    """

    commit: str | None
    timestamp: datetime


class DownwindSuiteFile(BaseModel):
    """Schema for .downwind.json."""

    version: int = 1
    generated_at: datetime
    generator_version: str = "0.1.0"
    changed_files: list[str]
    node_ids: list[str]
    profile: ProfileIdentityModel
    blind_spots: list[BlindSpotModel] = []

    @model_validator(mode="after")
    def _check_node_ids_and_blind_spots_not_both_populated(self) -> "DownwindSuiteFile":
        if self.node_ids and self.blind_spots:
            message = "node_ids and blind_spots must not both be non-empty"
            raise ValueError(message)
        return self


def write_downwind_suite(suite: DownwindSuiteFile, output_path: Path) -> None:
    """Write the downwind selection to a JSON file.

    node_ids is empty whenever blind_spots is non-empty: the full-suite
    fallback means "collect everything", which pytest already does without
    filtering, so there is nothing an enumerated node id list would add.
    """
    with output_path.open("w") as f:
        json.dump(suite.model_dump(mode="json"), f, indent=2)


def read_downwind_suite(path: Path) -> DownwindSuiteFile:
    """Read and validate a downwind selection file."""
    with path.open("rb") as f:
        data = json.load(f)
    return DownwindSuiteFile(**data)
