from smoke_optimiser.optimiser.filters import FilteredTests
from smoke_optimiser.optimiser.greedy import optimise
from smoke_optimiser.profiler.models import ProfilingOutcome

PRD_EXAMPLE_TOTAL_BRANCHES = 6
PRD_EXAMPLE_FULL_COVERAGE = 100.0
PRD_EXAMPLE_RUNTIME = 3.0
TIME_CAP_3S = 3.0
TIME_CAP_RUNTIME_2S = 2.0
TARGET_COV_50 = 50.0
FULL_SUITE_2_BRANCHES = 2


def _create_outcome(test_id: str, duration: float, branches: list[str]) -> ProfilingOutcome:
    return ProfilingOutcome(
        test_id=test_id,
        duration_s=duration,
        passed=True,
        branches_covered=frozenset(branches),
        markers=frozenset(),
    )


def test_optimise_prd_example() -> None:
    # PRD §6.4 Worked Example
    tests = {
        "test_a": _create_outcome("test_a", 1.0, ["b1", "b2", "b3"]),
        "test_b": _create_outcome("test_b", 1.0, ["b1", "b4"]),
        "test_c": _create_outcome("test_c", 1.0, ["b5"]),
        "test_d": _create_outcome("test_d", 1.0, ["b3", "b5", "b6"]),
        "test_e": _create_outcome("test_e", 10.0, ["b1", "b2", "b3", "b4", "b5"]),
    }
    total_branches = frozenset(["b1", "b2", "b3", "b4", "b5", "b6"])
    filtered = FilteredTests(
        candidates=tests,
        mandatory_included={},
        excluded={},
        failed={},
        unmatched_includes=[],
        unmatched_excludes=[],
    )

    # total_branches {b1..b6}, time_cap 5s, target_cov 100%
    result = optimise(filtered, total_branches, 5.0, 100.0)

    # Iteration 1: test_a wins (eff 3.0, alpha tiebreak over test_d)
    # Iteration 2: test_d wins (eff 2.0)
    # Iteration 3: test_b wins (eff 1.0, b4 only)
    selected_ids = [t.test_id for t in result.selected_tests]
    assert selected_ids == ["test_a", "test_d", "test_b"]
    assert result.smoke_branches_covered == PRD_EXAMPLE_TOTAL_BRANCHES
    assert result.smoke_coverage_pct == PRD_EXAMPLE_FULL_COVERAGE
    assert result.smoke_suite_runtime_s == PRD_EXAMPLE_RUNTIME
    assert result.full_suite_branches_covered == PRD_EXAMPLE_TOTAL_BRANCHES


def test_optimise_time_cap() -> None:
    tests = {
        "test_a": _create_outcome("test_a", 2.0, ["b1"]),
        "test_b": _create_outcome("test_b", 2.0, ["b2"]),
    }
    total_branches = frozenset(["b1", "b2"])
    filtered = FilteredTests(
        candidates=tests,
        mandatory_included={},
        excluded={},
        failed={},
        unmatched_includes=[],
        unmatched_excludes=[],
    )

    # Time cap 3.0s means only one test can be picked
    result = optimise(filtered, total_branches, TIME_CAP_3S, 100.0)
    assert len(result.selected_tests) == 1
    assert result.smoke_suite_runtime_s == TIME_CAP_RUNTIME_2S
    # Full suite covers 2, but smoke only picks 1
    assert result.full_suite_branches_covered == FULL_SUITE_2_BRANCHES


def test_optimise_target_cov() -> None:
    tests = {
        "test_a": _create_outcome("test_a", 1.0, ["b1"]),
        "test_b": _create_outcome("test_b", 1.0, ["b2"]),
    }
    total_branches = frozenset(["b1", "b2"])
    filtered = FilteredTests(
        candidates=tests,
        mandatory_included={},
        excluded={},
        failed={},
        unmatched_includes=[],
        unmatched_excludes=[],
    )

    # Target 50% means only one test is needed
    result = optimise(filtered, total_branches, 10.0, TARGET_COV_50)
    assert len(result.selected_tests) == 1
    assert result.full_suite_branches_covered == FULL_SUITE_2_BRANCHES


def test_optimise_tie_breaking() -> None:
    tests = {
        "test_1_lower_alpha": _create_outcome("test_1_lower_alpha", 1.0, ["b1"]),
        "test_2_higher_alpha": _create_outcome("test_2_higher_alpha", 1.0, ["b1"]),
    }
    total_branches = frozenset(["b1"])
    filtered = FilteredTests(
        candidates=tests,
        mandatory_included={},
        excluded={},
        failed={},
        unmatched_includes=[],
        unmatched_excludes=[],
    )

    result = optimise(filtered, total_branches, 10.0, 100.0)
    assert result.selected_tests[0].test_id == "test_1_lower_alpha"
    assert result.full_suite_branches_covered == 1


def test_optimise_coverage_equivalents() -> None:
    tests = {
        "test_a": _create_outcome("test_a", 1.0, ["b1"]),
        "test_b": _create_outcome("test_b", 1.0, ["b1"]),
    }
    total_branches = frozenset(["b1"])
    filtered = FilteredTests(
        candidates=tests,
        mandatory_included={},
        excluded={},
        failed={},
        unmatched_includes=[],
        unmatched_excludes=[],
    )

    result = optimise(filtered, total_branches, 10.0, 100.0)
    assert len(result.coverage_equivalents) == 1
    assert set(result.coverage_equivalents[0].tests) == {"test_a", "test_b"}
    assert result.full_suite_branches_covered == 1


def test_optimise_coverage_equivalents_deterministic_order() -> None:
    """Ensure equivalent groups are sorted deterministically by their string hash."""
    expected_equivalent_groups = 3
    tests = {
        # Group 1 (branches: b1)
        "test_a1": _create_outcome("test_a1", 1.0, ["b1"]),
        "test_a2": _create_outcome("test_a2", 1.0, ["b1"]),
        # Group 2 (branches: b2)
        "test_b1": _create_outcome("test_b1", 1.0, ["b2"]),
        "test_b2": _create_outcome("test_b2", 1.0, ["b2"]),
        # Group 3 (branches: b3)
        "test_c1": _create_outcome("test_c1", 1.0, ["b3"]),
        "test_c2": _create_outcome("test_c2", 1.0, ["b3"]),
    }
    total_branches = frozenset(["b1", "b2", "b3"])
    filtered = FilteredTests(
        candidates=tests,
        mandatory_included={},
        excluded={},
        failed={},
        unmatched_includes=[],
        unmatched_excludes=[],
    )

    result = optimise(filtered, total_branches, 10.0, 100.0)

    assert len(result.coverage_equivalents) == expected_equivalent_groups

    # Verify the IDs are assigned in a sorted hash order
    hashes = [eq.branch_set_hash for eq in result.coverage_equivalents]
    assert hashes == sorted(hashes)

    # Verify tests are also sorted within each group
    for eq in result.coverage_equivalents:
        assert list(eq.tests) == sorted(eq.tests)


def test_optimise_lazy_reevaluation() -> None:
    """Ensure that the lazy re-evaluation correctly handles overlapping coverage drops."""
    tests = {
        # T1 covers 4 branches, T2 covers 3, T3 covers 3.
        # Initially T1 wins. After T1 is selected, it covers b1, b2, b3, b4.
        # T2's remaining coverage drops to 1 (b5).
        # T3's remaining coverage drops to 2 (b6, b7).
        # So T3 must leapfrog T2 in the next iteration.
        "t1_initially_best": _create_outcome("t1_initially_best", 1.0, ["b1", "b2", "b3", "b4"]),
        "t2_overlaps_t1": _create_outcome("t2_overlaps_t1", 1.0, ["b2", "b3", "b5"]),
        "t3_disjoint_mostly": _create_outcome("t3_disjoint_mostly", 1.0, ["b1", "b6", "b7"]),
    }
    total_branches = frozenset(["b1", "b2", "b3", "b4", "b5", "b6", "b7"])
    filtered = FilteredTests(
        candidates=tests,
        mandatory_included={},
        excluded={},
        failed={},
        unmatched_includes=[],
        unmatched_excludes=[],
    )

    result = optimise(filtered, total_branches, 10.0, 100.0)

    # First: t1_initially_best (efficiency 4)
    # Remaining uncovered: b5, b6, b7
    # T2 marginal: 1 (b5)
    # T3 marginal: 2 (b6, b7) -> T3 should be selected second
    # T2 marginal: 1 -> T2 should be selected third
    selected_ids = [t.test_id for t in result.selected_tests]
    assert selected_ids == ["t1_initially_best", "t3_disjoint_mostly", "t2_overlaps_t1"]
