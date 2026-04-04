import hashlib
import heapq
from collections import defaultdict

from smoke_optimiser.optimiser.filters import FilteredTests
from smoke_optimiser.optimiser.models import (
    CoverageEquivalentGroup,
    SelectedTest,
    SmokeResult,
)

EFFICIENCY_EPSILON = 1e-9

# Indexes for candidate test attributes packed in a list.
# Packing into a list natively supported by heapq (comparing element by element)
# is much faster than using a custom Python __lt__ method.
IDX_EFFICIENCY = 0
IDX_MARGINAL = 1
IDX_DURATION = 2
IDX_TEST_ID = 3
IDX_BRANCHES = 4
IDX_ORIG_BRANCHES = 5
IDX_LAST_EVAL_COV_LEN = 6


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
    heap: list[list] = []

    # Pre-calculate candidate branches and populate the priority queue.
    for test in filtered.candidates.values():
        valid_branches = test.branches_covered & total_branches - covered_set
        if valid_branches:
            marginal = len(valid_branches)
            dur = test.duration_s
            # Use negative efficiency and negative marginal for max-heap behavior
            # round to avoid floating point precision issues that EFFICIENCY_EPSILON solved
            eff = marginal / dur if dur > 0 else (float("inf") if marginal > 0 else 0.0)
            eff_rounded = eff if eff == float("inf") else round(eff, 9)

            # Format: [-eff_rounded, -marginal, duration, test_id, branches, orig_branches, last_eval_cov_len]
            # We round -eff to 9 decimal places to mirror EFFICIENCY_EPSILON
            node = [
                -eff_rounded,
                -marginal,
                dur,
                test.test_id,
                set(valid_branches),
                test.branches_covered,
                len(covered_set),
            ]
            heapq.heappush(heap, node)

    target_count = (target_cov / 100.0) * len(total_branches)

    while heap and len(covered_set) < target_count:
        if len(total_branches) == len(covered_set):
            break

        node = heapq.heappop(heap)
        dur = node[IDX_DURATION]

        # Check if adding this test would exceed the time cap
        if elapsed_time + dur > time_cap:
            continue

        if node[IDX_LAST_EVAL_COV_LEN] == len(covered_set):
            # Up to date, it's the best!
            orig_branches = node[IDX_ORIG_BRANCHES]
            selected_tests.append(
                SelectedTest(
                    test_id=node[IDX_TEST_ID],
                    duration_s=dur,
                    branches_covered=len(orig_branches),
                    marginal_branches=-node[IDX_MARGINAL],
                    efficiency=-node[IDX_EFFICIENCY],
                )
            )
            covered_set.update(orig_branches)
            elapsed_time += dur
        else:
            # Re-evaluate
            remaining_branches = node[IDX_BRANCHES] - covered_set
            marginal = len(remaining_branches)
            if marginal > 0:
                eff = marginal / dur if dur > 0 else float("inf")
                eff_rounded = eff if eff == float("inf") else round(eff, 9)
                node[IDX_BRANCHES] = remaining_branches
                node[IDX_MARGINAL] = -marginal
                node[IDX_EFFICIENCY] = -eff_rounded
                node[IDX_LAST_EVAL_COV_LEN] = len(covered_set)
                heapq.heappush(heap, node)

    # 3. Stats and equivalents
    all_passed = {
        **filtered.candidates,
        **filtered.mandatory_included,
        **filtered.excluded,
    }

    # Use frozenset directly as dict key for fast C-level hashing and equality checks.
    # This avoids calculating an expensive stable string hash for every single test.
    branch_set_to_tests: dict[frozenset[str], list[str]] = defaultdict(list)

    # Calculate full suite coverage
    full_suite_coverage_set: set[str] = set()
    for test_id, outcome in all_passed.items():
        branch_set_to_tests[outcome.branches_covered].append(test_id)
        full_suite_coverage_set.update(outcome.branches_covered)

    equivalents = []
    group_id = 1

    # To maintain deterministic output, we must process the groups in a stable order.
    # We sort the groups by their stable string hash to ensure the JSON output is always the same.
    equivalent_groups = []
    for branches, test_ids in branch_set_to_tests.items():
        if len(test_ids) > 1:
            # Only compute the expensive string hash for actual equivalent groups
            h = _get_branch_set_hash(branches)
            equivalent_groups.append((h, tuple(sorted(test_ids))))

    # Sort by hash to guarantee deterministic group IDs
    equivalent_groups.sort(key=lambda x: x[0])

    for h, test_ids in equivalent_groups:
        equivalents.append(
            CoverageEquivalentGroup(
                group_id=group_id,
                branch_set_hash=h,
                tests=test_ids,
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
