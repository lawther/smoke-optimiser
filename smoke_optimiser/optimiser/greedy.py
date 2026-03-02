import hashlib
from collections import defaultdict

from smoke_optimiser.optimiser.filters import FilteredTests
from smoke_optimiser.optimiser.models import (
    CoverageEquivalentGroup,
    SelectedTest,
    SmokeResult,
)

EFFICIENCY_EPSILON = 1e-9


def _get_branch_set_hash(branches: frozenset[str]) -> str:
    """Generate a stable hash for a set of branches."""
    sorted_branches = sorted(branches)
    return hashlib.sha256(",".join(sorted_branches).encode()).hexdigest()


def optimise(
    filtered: FilteredTests,
    total_branches: frozenset[str],
    time_cap: float,
    target_cov: float,
) -> SmokeResult:
    """Select a subset of tests using a greedy weighted set-cover approximation."""
    selected_tests: list[SelectedTest] = []
    covered_set: set[str] = set()
    elapsed_time = 0.0

    # 1. Pre-population from mandatory_included
    for test_id, outcome in filtered.mandatory_included.items():
        marginal = len(outcome.branches_covered - covered_set)
        efficiency = marginal / outcome.duration_s if outcome.duration_s > 0 else 0.0

        selected_tests.append(
            SelectedTest(
                test_id=test_id,
                duration_s=outcome.duration_s,
                branches_covered=len(outcome.branches_covered),
                marginal_branches=marginal,
                efficiency=efficiency,
            )
        )
        covered_set.update(outcome.branches_covered)
        elapsed_time += outcome.duration_s

    # 2. Greedy selection
    candidates = list(filtered.candidates.values())
    target_count = (target_cov / 100.0) * len(total_branches)

    while len(covered_set) < target_count:
        uncovered = total_branches - covered_set
        if not uncovered:
            break

        best_test = None
        best_efficiency = -1.0
        best_marginal = -1
        best_duration = float("inf")

        for test in candidates:
            # Check if adding this test would exceed the time cap
            if elapsed_time + test.duration_s > time_cap:
                continue

            marginal_set = test.branches_covered & uncovered
            marginal = len(marginal_set)
            efficiency = marginal / test.duration_s if test.duration_s > 0 else 0.0

            # Tie-breaking: higher efficiency -> higher marginal -> shorter duration -> alpha test_id
            is_better = efficiency > best_efficiency or (
                abs(efficiency - best_efficiency) < EFFICIENCY_EPSILON
                and (
                    marginal > best_marginal
                    or (
                        marginal == best_marginal
                        and (
                            test.duration_s < best_duration
                            or (
                                test.duration_s == best_duration
                                and (best_test is None or test.test_id < best_test.test_id)
                            )
                        )
                    )
                )
            )

            if is_better:
                best_test = test
                best_efficiency = efficiency
                best_marginal = marginal
                best_duration = test.duration_s

        if best_test is None or best_marginal == 0:
            break

        selected_tests.append(
            SelectedTest(
                test_id=best_test.test_id,
                duration_s=best_test.duration_s,
                branches_covered=len(best_test.branches_covered),
                marginal_branches=best_marginal,
                efficiency=best_efficiency,
            )
        )
        covered_set.update(best_test.branches_covered)
        elapsed_time += best_test.duration_s
        candidates.remove(best_test)

    # 3. Stats and equivalents
    all_passed = {
        **filtered.candidates,
        **filtered.mandatory_included,
        **filtered.excluded,
    }
    hash_to_tests = defaultdict(list)
    for test_id, outcome in all_passed.items():
        h = _get_branch_set_hash(outcome.branches_covered)
        hash_to_tests[h].append(test_id)

    equivalents = []
    group_id = 1
    for h, test_ids in hash_to_tests.items():
        if len(test_ids) > 1:
            equivalents.append(
                CoverageEquivalentGroup(
                    group_id=group_id,
                    branch_set_hash=h,
                    tests=tuple(sorted(test_ids)),
                )
            )
            group_id += 1

    total_tests_profiled = (
        len(filtered.candidates) + len(filtered.mandatory_included) + len(filtered.excluded) + len(filtered.failed)
    )
    tests_passed = len(all_passed)
    full_suite_runtime = sum(t.duration_s for t in all_passed.values()) + sum(
        t.duration_s for t in filtered.failed.values()
    )

    return SmokeResult(
        selected_tests=selected_tests,
        total_tests_profiled=total_tests_profiled,
        tests_passed=tests_passed,
        tests_failed=len(filtered.failed),
        total_branches=len(total_branches),
        smoke_branches_covered=len(covered_set),
        smoke_coverage_pct=(len(covered_set) / len(total_branches) * 100.0) if total_branches else 0.0,
        full_suite_runtime_s=full_suite_runtime,
        smoke_suite_runtime_s=elapsed_time,
        coverage_equivalents=equivalents,
    )
