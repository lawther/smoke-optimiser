from smoke_optimiser.optimiser.filters import FilteredTests
from smoke_optimiser.optimiser.greedy import optimise
from smoke_optimiser.profiler.models import ProfilingOutcome

PRD_EXAMPLE_TOTAL_BRANCHES = 6
PRD_EXAMPLE_FULL_COVERAGE = 100.0
PRD_EXAMPLE_RUNTIME = 3.0
TIME_CAP_3S = 3.0
TIME_CAP_RUNTIME_2S = 2.0
TARGET_COV_50 = 50.0


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
    filtered = FilteredTests(candidates=tests, mandatory_included={}, excluded={}, failed={})

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


def test_optimise_time_cap() -> None:
    tests = {
        "test_a": _create_outcome("test_a", 2.0, ["b1"]),
        "test_b": _create_outcome("test_b", 2.0, ["b2"]),
    }
    total_branches = frozenset(["b1", "b2"])
    filtered = FilteredTests(candidates=tests, mandatory_included={}, excluded={}, failed={})

    # Time cap 3.0s means only one test can be picked
    result = optimise(filtered, total_branches, TIME_CAP_3S, 100.0)
    assert len(result.selected_tests) == 1
    assert result.smoke_suite_runtime_s == TIME_CAP_RUNTIME_2S


def test_optimise_target_cov() -> None:
    tests = {
        "test_a": _create_outcome("test_a", 1.0, ["b1"]),
        "test_b": _create_outcome("test_b", 1.0, ["b2"]),
    }
    total_branches = frozenset(["b1", "b2"])
    filtered = FilteredTests(candidates=tests, mandatory_included={}, excluded={}, failed={})

    # Target 50% means only one test is needed
    result = optimise(filtered, total_branches, 10.0, TARGET_COV_50)
    assert len(result.selected_tests) == 1


def test_optimise_tie_breaking() -> None:
    tests = {
        "test_1_lower_alpha": _create_outcome("test_1_lower_alpha", 1.0, ["b1"]),
        "test_2_higher_alpha": _create_outcome("test_2_higher_alpha", 1.0, ["b1"]),
    }
    total_branches = frozenset(["b1"])
    filtered = FilteredTests(candidates=tests, mandatory_included={}, excluded={}, failed={})

    result = optimise(filtered, total_branches, 10.0, 100.0)
    assert result.selected_tests[0].test_id == "test_1_lower_alpha"


def test_optimise_coverage_equivalents() -> None:
    tests = {
        "test_a": _create_outcome("test_a", 1.0, ["b1"]),
        "test_b": _create_outcome("test_b", 1.0, ["b1"]),
    }
    total_branches = frozenset(["b1"])
    filtered = FilteredTests(candidates=tests, mandatory_included={}, excluded={}, failed={})

    result = optimise(filtered, total_branches, 10.0, 100.0)
    assert len(result.coverage_equivalents) == 1
    assert set(result.coverage_equivalents[0].tests) == {"test_a", "test_b"}
