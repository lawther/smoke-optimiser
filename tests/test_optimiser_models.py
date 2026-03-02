import pytest

from smoke_optimiser.optimiser.models import (
    CoverageEquivalentGroup,
    SelectedTest,
    SmokeResult,
)

TOTAL_TESTS = 100
PASSED_TESTS = 95
FAILED_TESTS = 5
TOTAL_BRANCHES = 1000
FULL_SUITE_BRANCHES = 900
SMOKE_BRANCHES = 800
SMOKE_COVERAGE = 80.0
FULL_RUNTIME = 300.0
SMOKE_RUNTIME = 30.0


def test_selected_test_construction() -> None:
    st = SelectedTest(
        test_id="test_a",
        duration_s=0.1,
        branches_covered=10,
        marginal_branches=10,
        efficiency=100.0,
    )
    assert st.test_id == "test_a"
    with pytest.raises(AttributeError):
        st.efficiency = 200.0  # type: ignore[misc]


def test_smoke_result_construction() -> None:
    sr = SmokeResult(
        selected_tests=[],
        total_tests_profiled=TOTAL_TESTS,
        tests_passed=PASSED_TESTS,
        tests_failed=FAILED_TESTS,
        total_branches=TOTAL_BRANCHES,
        full_suite_branches_covered=FULL_SUITE_BRANCHES,
        smoke_branches_covered=SMOKE_BRANCHES,
        smoke_coverage_pct=SMOKE_COVERAGE,
        full_suite_runtime_s=FULL_RUNTIME,
        smoke_suite_runtime_s=SMOKE_RUNTIME,
        coverage_equivalents=[],
    )
    assert sr.total_tests_profiled == TOTAL_TESTS
    assert sr.smoke_coverage_pct == SMOKE_COVERAGE
    assert sr.full_suite_branches_covered == FULL_SUITE_BRANCHES


def test_coverage_equivalent_group() -> None:
    ceg = CoverageEquivalentGroup(
        group_id=1,
        branch_set_hash="abc",
        tests=("test_a", "test_b"),
    )
    assert ceg.group_id == 1
    assert ceg.tests == ("test_a", "test_b")
