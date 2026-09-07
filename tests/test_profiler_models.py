from datetime import UTC, datetime
from typing import Any, cast

import pytest
from pydantic import ValidationError

from smoke_optimiser.profiler.models import (
    PROFILE_SCHEMA_VERSION,
    ImportEdge,
    ProfileSchemaMismatchError,
    ProfilingData,
    ProfilingDataFile,
    ProfilingOutcome,
    load_profiling_data_file,
)


def test_profiling_outcome_construction() -> None:
    tr = ProfilingOutcome(
        test_id="test_a",
        duration_s=0.1,
        passed=True,
        branches_covered=frozenset(["file.py:10", "file.py:12"]),
        files_covered=frozenset(["file.py"]),
        markers=frozenset(["smoke"]),
    )
    assert tr.test_id == "test_a"
    assert "smoke" in tr.markers
    with pytest.raises((AttributeError, Exception)):
        tr.passed = False  # ty: ignore[invalid-assignment] - verifying immutability


def test_profiling_data_roundtrip() -> None:
    raw_data = {
        "schema_version": PROFILE_SCHEMA_VERSION,
        "meta": {
            "timestamp": "2026-03-02T10:30:00Z",
            "commit": "abcdef",
            "python_version": "3.12",
            "coverage_version": "7.0",
            "command": "smoke-optimiser",
            "machine": {
                "os": "Linux",
                "os_version": "6.5",
                "platform": "Ubuntu",
                "architecture": "x86_64",
                "cpu_model": "AMD",
                "cpu_cores_physical": 16,
                "cpu_cores_logical": 32,
                "ram_total_mb": 65536,
                "ram_available_mb": 58200,
                "hostname": "ci-04",
            },
            "xdist_workers": 1,
        },
        "tests": {
            "test_a": {
                "test_id": "test_a",
                "duration_s": 0.1,
                "passed": True,
                "branches_covered": ["file.py:10"],
                "files_covered": ["file.py"],
                "markers": ["smoke"],
            },
        },
        "total_branches": ["file.py:10", "file.py:11"],
        "measured_files": ["file.py", "other.py"],
        "import_graph": {
            "edges": [{"importer": "tests/test_app.py", "imported": "file.py"}],
            "unattributed_modules": ["tests/test_app.py"],
            "resolution_errors": 0,
            "error_samples": [],
        },
    }

    model = ProfilingDataFile(**cast("Any", raw_data))
    data = model.to_profiling_data()

    assert isinstance(data, ProfilingData)
    assert data.meta.timestamp == datetime(2026, 3, 2, 10, 30, 0, tzinfo=UTC)
    assert data.meta.machine.os == "Linux"
    assert data.tests["test_a"].test_id == "test_a"
    assert data.import_graph.edges == frozenset({ImportEdge(importer="tests/test_app.py", imported="file.py")})
    assert data.import_graph.unattributed_modules == frozenset({"tests/test_app.py"})
    assert data.total_branches == frozenset(["file.py:10", "file.py:11"])
    assert data.tests["test_a"].files_covered == frozenset(["file.py"])
    assert data.measured_files == frozenset(["file.py", "other.py"])


def test_a_profile_written_before_file_tracking_is_refused() -> None:
    """Profiles predating files_covered must fail loudly, not load as empty.

    An empty file set is indistinguishable from 'no test touches this file',
    which would let change-based selection silently skip tests.
    """
    raw_data = {
        "meta": {
            "timestamp": "2026-03-02T10:30:00Z",
            "commit": None,
            "python_version": "3.12",
            "coverage_version": "7.0",
            "command": "smoke-optimiser",
            "machine": {},
        },
        "tests": {
            "test_a": {
                "test_id": "test_a",
                "duration_s": 0.1,
                "passed": True,
                "branches_covered": ["file.py:10"],
                "markers": [],
            },
        },
        "total_branches": ["file.py:10"],
    }

    with pytest.raises(ValidationError) as exc_info:
        ProfilingDataFile(**cast("Any", raw_data))

    message = str(exc_info.value)
    assert "files_covered" in message
    assert "measured_files" in message


def test_profiling_data_validation_error() -> None:
    # Missing required field
    with pytest.raises(ValidationError):
        # Use cast(Any, ...) to avoid ty's type check for intentionally invalid inputs
        ProfilingDataFile(**cast("Any", {"meta": {}, "tests": {}, "total_branches": []}))


def test_schema_version_has_no_default() -> None:
    """A pre-versioning profile must fail to validate, not silently read as current.

    Copying .smoke_suite.json's `version: int = 1` default would make a file
    with no version field validate as version 1, defeating the point of
    adding the field.
    """
    with pytest.raises(ValidationError) as exc_info:
        # Use cast(Any, ...) to avoid ty's type check for intentionally invalid inputs
        ProfilingDataFile(**cast("Any", {"meta": {}, "tests": {}, "total_branches": []}))

    assert "schema_version" in str(exc_info.value)


def test_load_profiling_data_file_rejects_a_schema_version_mismatch_before_pydantic_runs() -> None:
    """A stale schema_version must be reported as a mismatch, not a ValidationError.

    Distinguishing the two is the entire point of load_profiling_data_file:
    an old profile can also be missing fields Pydantic would report, but the
    fix for both is the same ("reprofile"), and the mismatch is the more
    accurate diagnosis.
    """
    raw = {"schema_version": PROFILE_SCHEMA_VERSION - 1, "meta": {}, "tests": {}, "total_branches": []}

    with pytest.raises(ProfileSchemaMismatchError) as exc_info:
        load_profiling_data_file(cast("Any", raw))

    assert exc_info.value.found == PROFILE_SCHEMA_VERSION - 1
    assert exc_info.value.expected == PROFILE_SCHEMA_VERSION


def test_load_profiling_data_file_reports_a_missing_schema_version_as_a_mismatch() -> None:
    """A profile predating the field entirely must also raise ProfileSchemaMismatchError, not ValidationError."""
    raw = {"meta": {}, "tests": {}, "total_branches": []}

    with pytest.raises(ProfileSchemaMismatchError) as exc_info:
        load_profiling_data_file(cast("Any", raw))

    assert exc_info.value.found is None
    assert exc_info.value.expected == PROFILE_SCHEMA_VERSION
