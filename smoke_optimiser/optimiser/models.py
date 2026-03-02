from dataclasses import dataclass


@dataclass(frozen=True)
class SelectedTest:
    """A test selected for the smoke suite."""

    test_id: str
    duration_s: float
    branches_covered: int
    marginal_branches: int
    efficiency: float


@dataclass(frozen=True)
class CoverageEquivalentGroup:
    """Group of tests that cover the exact same set of branches."""

    group_id: int
    branch_set_hash: str
    tests: tuple[str, ...]


@dataclass(frozen=True)
class SmokeResult:
    """Final result of the optimisation process."""

    selected_tests: list[SelectedTest]
    total_tests_profiled: int
    tests_passed: int
    tests_failed: int
    total_branches: int
    full_suite_branches_covered: int
    smoke_branches_covered: int
    smoke_coverage_pct: float
    full_suite_runtime_s: float
    smoke_suite_runtime_s: float
    coverage_equivalents: list[CoverageEquivalentGroup]
