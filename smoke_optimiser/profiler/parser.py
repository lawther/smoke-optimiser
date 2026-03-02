from datetime import UTC, datetime
from pathlib import Path

import ijson

from smoke_optimiser.environment import capture_environment
from smoke_optimiser.profiler.models import (
    ProfilingData,
    ProfilingMeta,
    ProfilingOutcome,
)


def parse_coverage_json(
    coverage_json_path: Path,
    test_durations: dict[str, float],
    test_outcomes: dict[str, bool],
    test_markers: dict[str, frozenset[str]],
) -> ProfilingData:
    """Parse coverage.py JSON output using ijson for streaming.

    Maps coverage contexts to test IDs and aggregates results.
    """
    tests: dict[str, set[str]] = {}
    total_branches: set[str] = set()

    with open(coverage_json_path, "rb") as f:
        # First, let's get the coverage version and other meta if possible
        f.seek(0)
        objects = ijson.items(f, "")
        full_json = next(objects)

        coverage_version = full_json.get("meta", {}).get("version", "unknown")

        for file_path, file_data in full_json.get("files", {}).items():
            # Collect total branches in the suite
            executed = file_data.get("executed_branches", [])
            missing = file_data.get("missing_branches", [])
            for br in executed + missing:
                total_branches.add(f"{file_path}:{br[0]}->{br[1]}")

            # Collect per-test coverage
            contexts = file_data.get("contexts", {})
            for test_id, context_data in contexts.items():
                if test_id == "":  # Skip empty context (base coverage)
                    continue

                if test_id not in tests:
                    tests[test_id] = set()

                for br in context_data.get("executed_branches", []):
                    tests[test_id].add(f"{file_path}:{br[0]}->{br[1]}")

    profiling_outcomes = {}
    # Combine coverage with durations and outcomes
    # We use test_durations keys as the source of truth for all tests that ran
    for test_id, duration in test_durations.items():
        branches = frozenset(tests.get(test_id, set()))
        profiling_outcomes[test_id] = ProfilingOutcome(
            test_id=test_id,
            duration_s=duration,
            passed=test_outcomes.get(test_id, False),
            branches_covered=branches,
            markers=test_markers.get(test_id, frozenset()),
        )

    meta = ProfilingMeta(
        timestamp=datetime.now(UTC),
        commit=None,  # Will be filled by runner
        python_version="3.12",  # Will be filled by runner
        coverage_version=str(coverage_version),
        command="",  # Will be filled by runner
        machine=capture_environment(),
    )

    return ProfilingData(
        meta=meta,
        tests=profiling_outcomes,
        total_branches=frozenset(total_branches),
    )
