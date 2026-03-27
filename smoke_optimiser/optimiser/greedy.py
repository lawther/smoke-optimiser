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

    # Pre-calculate candidate branches and keep them in a mutable list.
    # We dynamically remove branches that are already covered to avoid computing
    # `test.branches_covered & uncovered` on every iteration.
    candidates = []
    for test in filtered.candidates.values():
        # Only track branches that are part of the target population and not already covered
        valid_branches = test.branches_covered & total_branches - covered_set
        if valid_branches:
            candidates.append([test.test_id, set(valid_branches), test.duration_s, test.branches_covered])

    target_count = (target_cov / 100.0) * len(total_branches)

    while len(covered_set) < target_count:
        if len(total_branches) == len(covered_set):
            break

        best_idx = -1
        best_efficiency = -1.0
        best_marginal = -1
        best_duration = float("inf")
        best_test_id = ""

        for idx, candidate in enumerate(candidates):
            if candidate is None:
                continue

            test_id, branches, duration, orig_branches = candidate

            # Check if adding this test would exceed the time cap
            if elapsed_time + duration > time_cap:
                continue

            marginal = len(branches)
            if marginal == 0:
                # Test provides no more marginal coverage; drop it from future checks
                candidates[idx] = None
                continue

            # If duration is 0, it's infinitely efficient if it has marginal coverage
            efficiency = marginal / duration if duration > 0 else (float("inf") if marginal > 0 else 0.0)

            # Tie-breaking: higher efficiency -> higher marginal -> shorter duration -> alpha test_id
            is_better = efficiency > best_efficiency or (
                abs(efficiency - best_efficiency) < EFFICIENCY_EPSILON
                and (
                    marginal > best_marginal
                    or (
                        marginal == best_marginal
                        and (
                            duration < best_duration
                            or (duration == best_duration and (best_idx == -1 or test_id < best_test_id))
                        )
                    )
                )
            )

            if is_better:
                best_idx = idx
                best_efficiency = efficiency
                best_marginal = marginal
                best_duration = duration
                best_test_id = test_id

        if best_idx == -1 or best_marginal == 0:
            break

        winner = candidates[best_idx]
        test_id, branches, duration, orig_branches = winner

        selected_tests.append(
            SelectedTest(
                test_id=test_id,
                duration_s=duration,
                branches_covered=len(orig_branches),
                marginal_branches=best_marginal,
                efficiency=best_efficiency,
            )
        )

        # Keep track of what was just added to dynamically subtract from candidates
        new_covered = branches.copy()

        # Ensure we add ALL originally covered branches to the global tracking set
        covered_set.update(orig_branches)
        elapsed_time += duration

        # Remove the selected test
        candidates[best_idx] = None

        # Update remaining candidates efficiently in-place
        for candidate in candidates:
            if candidate is not None:
                candidate[1] -= new_covered

    # 3. Stats and equivalents
    all_passed = {
        **filtered.candidates,
        **filtered.mandatory_included,
        **filtered.excluded,
    }

    # Pre-group tests by the exact set of branches they cover.
    # frozensets are hashable, so we can use them as dict keys directly
    # instead of performing expensive string sorting and SHA-256 hashing
    # on every single test.
    set_to_tests = defaultdict(list)
    for test_id, outcome in all_passed.items():
        set_to_tests[outcome.branches_covered].append(test_id)

    # Calculate full suite coverage
    full_suite_coverage_set: set[str] = set()

    equivalents = []
    group_id = 1
    for branches, test_ids in set_to_tests.items():
        # Update full suite coverage only once per unique set of branches
        full_suite_coverage_set.update(branches)

        # Only compute the expensive branch set hash if we actually have
        # an equivalent group (i.e. > 1 test covering the exact same branches)
        if len(test_ids) > 1:
            h = _get_branch_set_hash(branches)
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
        full_suite_branches_covered=len(full_suite_coverage_set),
        smoke_branches_covered=len(covered_set),
        smoke_coverage_pct=(len(covered_set) / len(total_branches) * 100.0) if total_branches else 0.0,
        full_suite_runtime_s=full_suite_runtime,
        smoke_suite_runtime_s=elapsed_time,
        coverage_equivalents=equivalents,
    )
