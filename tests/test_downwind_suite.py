from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from smoke_optimiser.reports.downwind_suite import (
    BlindSpotModel,
    BlindSpotReason,
    DownwindSuiteFile,
    ProfileIdentityModel,
    read_downwind_suite,
    write_downwind_suite,
)


def _profile_identity() -> ProfileIdentityModel:
    return ProfileIdentityModel(commit="abcdef", timestamp=datetime(2026, 3, 2, 10, 30, 0, tzinfo=UTC))


def test_downwind_suite_roundtrip_selected(tmp_path: Path) -> None:
    suite = DownwindSuiteFile(
        generated_at=datetime(2026, 9, 7, 9, 0, 0, tzinfo=UTC),
        changed_files=["smoke_optimiser/downwind/maps.py"],
        node_ids=["tests/test_downwind_maps.py::test_knows"],
        profile=_profile_identity(),
        blind_spots=[],
    )
    output_file = tmp_path / ".downwind.json"
    write_downwind_suite(suite, output_file)

    loaded = read_downwind_suite(output_file)
    assert loaded.node_ids == ["tests/test_downwind_maps.py::test_knows"]
    assert loaded.blind_spots == []
    assert loaded.profile.commit == "abcdef"


def test_downwind_suite_roundtrip_blind_spot(tmp_path: Path) -> None:
    suite = DownwindSuiteFile(
        generated_at=datetime(2026, 9, 7, 9, 0, 0, tzinfo=UTC),
        changed_files=["smoke_optimiser/new_module.py"],
        node_ids=[],
        profile=_profile_identity(),
        blind_spots=[BlindSpotModel(reason=BlindSpotReason.UNKNOWN_PATH, file="smoke_optimiser/new_module.py")],
    )
    output_file = tmp_path / ".downwind.json"
    write_downwind_suite(suite, output_file)

    loaded = read_downwind_suite(output_file)
    assert loaded.node_ids == []
    assert loaded.blind_spots[0].reason == BlindSpotReason.UNKNOWN_PATH
    assert loaded.blind_spots[0].file == "smoke_optimiser/new_module.py"


RESOLUTION_ERROR_COUNT = 3


def test_downwind_suite_resolution_errors_blind_spot_has_no_file(tmp_path: Path) -> None:
    suite = DownwindSuiteFile(
        generated_at=datetime(2026, 9, 7, 9, 0, 0, tzinfo=UTC),
        changed_files=["smoke_optimiser/downwind/maps.py"],
        node_ids=[],
        profile=_profile_identity(),
        blind_spots=[
            BlindSpotModel(reason=BlindSpotReason.RESOLUTION_ERRORS, resolution_errors=RESOLUTION_ERROR_COUNT),
        ],
    )
    output_file = tmp_path / ".downwind.json"
    write_downwind_suite(suite, output_file)

    loaded = read_downwind_suite(output_file)
    assert loaded.blind_spots[0].file is None
    assert loaded.blind_spots[0].resolution_errors == RESOLUTION_ERROR_COUNT


def test_blind_spot_model_rejects_file_with_resolution_errors_reason() -> None:
    with pytest.raises(ValidationError):
        BlindSpotModel(
            reason=BlindSpotReason.RESOLUTION_ERRORS,
            file="smoke_optimiser/new_module.py",
            resolution_errors=RESOLUTION_ERROR_COUNT,
        )


def test_blind_spot_model_rejects_missing_file_for_non_resolution_errors_reason() -> None:
    with pytest.raises(ValidationError):
        BlindSpotModel(reason=BlindSpotReason.UNKNOWN_PATH)


def test_blind_spot_model_rejects_resolution_errors_count_for_other_reasons() -> None:
    with pytest.raises(ValidationError):
        BlindSpotModel(
            reason=BlindSpotReason.UNKNOWN_PATH,
            file="smoke_optimiser/new_module.py",
            resolution_errors=RESOLUTION_ERROR_COUNT,
        )


def test_blind_spot_model_rejects_missing_resolution_errors_count() -> None:
    with pytest.raises(ValidationError):
        BlindSpotModel(reason=BlindSpotReason.RESOLUTION_ERRORS)


def test_downwind_suite_file_rejects_node_ids_and_blind_spots_both_populated() -> None:
    with pytest.raises(ValidationError):
        DownwindSuiteFile(
            generated_at=datetime(2026, 9, 7, 9, 0, 0, tzinfo=UTC),
            changed_files=["smoke_optimiser/new_module.py"],
            node_ids=["tests/test_downwind_maps.py::test_knows"],
            profile=_profile_identity(),
            blind_spots=[BlindSpotModel(reason=BlindSpotReason.UNKNOWN_PATH, file="smoke_optimiser/new_module.py")],
        )


def test_downwind_suite_rejects_unknown_reason(tmp_path: Path) -> None:
    output_file = tmp_path / ".downwind.json"
    output_file.write_text(
        """
        {
            "version": 1,
            "generated_at": "2026-09-07T09:00:00Z",
            "changed_files": [],
            "node_ids": [],
            "profile": {"commit": "abcdef", "timestamp": "2026-09-07T09:00:00Z"},
            "blind_spots": [{"reason": "not_a_real_reason"}]
        }
        """,
    )
    with pytest.raises(ValidationError):
        read_downwind_suite(output_file)
