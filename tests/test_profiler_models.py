from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from smoke_optimiser.profiler.models import (
    ProfilingData,
    ProfilingDataFile,
    ProfilingOutcome,
)


def test_profiling_outcome_construction() -> None:
    tr = ProfilingOutcome(
        test_id="test_a",
        duration_s=0.1,
        passed=True,
        branches_covered=frozenset(["file.py:10", "file.py:12"]),
        markers=frozenset(["smoke"]),
    )
    assert tr.test_id == "test_a"
    assert "smoke" in tr.markers
    with pytest.raises(AttributeError):
        tr.passed = False  # type: ignore[misc]


def test_profiling_data_roundtrip() -> None:
    raw_data = {
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
        },
        "tests": {
            "test_a": {
                "test_id": "test_a",
                "duration_s": 0.1,
                "passed": True,
                "branches_covered": ["file.py:10"],
                "markers": ["smoke"],
            }
        },
        "total_branches": ["file.py:10", "file.py:11"],
    }

    model = ProfilingDataFile(**raw_data)
    data = model.to_profiling_data()

    assert isinstance(data, ProfilingData)
    assert data.meta.timestamp == datetime(2026, 3, 2, 10, 30, 0, tzinfo=UTC)
    assert data.meta.machine.os == "Linux"
    assert data.tests["test_a"].test_id == "test_a"
    assert data.total_branches == frozenset(["file.py:10", "file.py:11"])


def test_profiling_data_validation_error() -> None:
    # Missing required field
    with pytest.raises(ValidationError):
        ProfilingDataFile(meta={}, tests={}, total_branches=[])  # type: ignore[arg-type]
