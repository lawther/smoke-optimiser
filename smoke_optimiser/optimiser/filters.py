import fnmatch
from dataclasses import dataclass

from smoke_optimiser.profiler.models import ProfilingOutcome


@dataclass(frozen=True)
class FilteredTests:
    """Grouped tests after applying filters."""

    candidates: dict[str, ProfilingOutcome]
    mandatory_included: dict[str, ProfilingOutcome]
    excluded: dict[str, ProfilingOutcome]
    failed: dict[str, ProfilingOutcome]


def _matches_pattern(test_id: str, markers: frozenset[str], pattern: str) -> bool:
    """Check if a test matches a glob pattern or marker pattern."""
    if pattern.startswith("@pytest.mark."):
        marker_name = pattern[len("@pytest.mark.") :]
        return marker_name in markers
    return fnmatch.fnmatch(test_id, pattern)


def apply_filters(
    tests: dict[str, ProfilingOutcome],
    include_mandatory: list[str],
    exclude_mandatory: list[str],
) -> FilteredTests:
    """Split tests into candidates, mandatory included, excluded, and failed.

    1. Failed tests are always hard-excluded.
    2. Exclude mandatory patterns take precedence.
    3. Include mandatory patterns force inclusion.
    """
    candidates = {}
    mandatory_included = {}
    excluded = {}
    failed = {}

    for test_id, outcome in tests.items():
        if not outcome.passed:
            failed[test_id] = outcome
            continue

        # Check exclude first (precedence)
        is_excluded = False
        for pattern in exclude_mandatory:
            if _matches_pattern(test_id, outcome.markers, pattern):
                excluded[test_id] = outcome
                is_excluded = True
                break

        if is_excluded:
            continue

        # Check include
        is_mandatory = False
        for pattern in include_mandatory:
            if _matches_pattern(test_id, outcome.markers, pattern):
                mandatory_included[test_id] = outcome
                is_mandatory = True
                break

        if not is_mandatory:
            candidates[test_id] = outcome

    return FilteredTests(
        candidates=candidates,
        mandatory_included=mandatory_included,
        excluded=excluded,
        failed=failed,
    )
