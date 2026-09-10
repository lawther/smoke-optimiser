from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest

from smoke_optimiser.environment import MachineEnvironment
from smoke_optimiser.profiler.models import (
    PROFILE_SCHEMA_VERSION,
    ImportGraph,
    ProfilingData,
    ProfilingMeta,
    ReadMap,
)
from smoke_optimiser.profiler.scope import ProfileScope

type RawProfileFactory = Callable[[], dict[str, Any]]

pytest_plugins = ["pytester"]


@pytest.fixture
def empty_graph() -> ImportGraph:
    """An import graph that recorded nothing, for tests that are not about the graph."""
    return ImportGraph(
        edges=frozenset(),
        unattributed_modules=frozenset(),
        resolution_errors=0,
        error_samples=(),
    )


@pytest.fixture
def empty_read_map() -> ReadMap:
    """A read map that recorded nothing, for tests that are not about reads."""
    return ReadMap(
        reads_by_test={},
        listings_by_test={},
        unattributed_reads=frozenset(),
        recording_errors=0,
        error_samples=(),
    )


@pytest.fixture
def raw_profile() -> RawProfileFactory:
    """Build a complete profile of the current schema, as JSON would hold it.

    A factory rather than a value: tests mutate it to describe the one thing
    that is wrong with the file they are about.
    """

    def _build() -> dict[str, Any]:
        return {
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
                "iterations": 1,
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
            "scope": {
                "coverage_roots": ["src"],
                "test_roots": ["tests"],
                "test_file_patterns": ["test_*.py"],
                "include_namespace_packages": False,
            },
            "import_graph": {
                "edges": [{"importer": "tests/test_app.py", "imported": "file.py"}],
                "unattributed_modules": ["tests/test_app.py"],
                "resolution_errors": 0,
                "error_samples": [],
            },
        }

    return _build


@pytest.fixture
def profiled_suite() -> ProfilingData:
    """A minimal but genuine profile, for tests that stub out the profiling phase.

    A ``MagicMock`` will not do here: every ``smoke`` run now saves the profile,
    and saving validates it, so a stubbed profiling phase has to hand back
    something a Pydantic model will accept.
    """
    return ProfilingData(
        meta=ProfilingMeta(
            timestamp=datetime(2026, 3, 2, 10, 30, 0, tzinfo=UTC),
            commit="abcdef",
            python_version="3.12",
            coverage_version="7.0",
            command="smoke-optimiser smoke",
            machine=MachineEnvironment(
                os="Linux",
                os_version="6.5",
                platform="Ubuntu",
                architecture="x86_64",
                cpu_model="AMD",
                cpu_cores_physical=16,
                cpu_cores_logical=32,
                ram_total_mb=65536,
                ram_available_mb=58200,
                hostname="ci-04",
            ),
            xdist_workers=1,
            iterations=1,
        ),
        tests={},
        total_branches=frozenset(),
        measured_files=frozenset(),
        import_graph=ImportGraph(
            edges=frozenset(),
            unattributed_modules=frozenset(),
            resolution_errors=0,
            error_samples=(),
        ),
        scope=ProfileScope(
            coverage_roots=frozenset({"src"}),
            test_roots=frozenset({"tests"}),
            test_file_patterns=("test_*.py",),
        ),
    )
