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


class _CandidateNode:
    """Represents a candidate test in the priority queue for lazy greedy selection."""

    __slots__ = (
        "branches",
        "duration",
        "efficiency",
        "last_eval_cov_len",
        "marginal",
        "orig_branches",
        "test_id",
    )

    def __init__(
        self,
        test_id: str,
        branches: set[str],
        duration: float,
        orig_branches: frozenset[str],
        efficiency: float,
        marginal: int,
        last_eval_cov_len: int,
    ) -> None:
        self.test_id = test_id
        self.branches = branches
        self.duration = duration
        self.orig_branches = orig_branches
        self.efficiency = efficiency
        self.marginal = marginal
        self.last_eval_cov_len = last_eval_cov_len

    def __lt__(self, other: "_CandidateNode") -> bool:
        # We want the BEST candidate to be SMALLER for heapq (max heap simulation)
        eff_diff = self.efficiency - other.efficiency
        if abs(eff_diff) > EFFICIENCY_EPSILON:
            return self.efficiency > other.efficiency
        if self.marginal != other.marginal:
            return self.marginal > other.marginal
        if self.duration != other.duration:
            return self.duration < other.duration
        return self.test_id < other.test_id


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
    heap: list[_CandidateNode] = []

    # Pre-calculate candidate branches and populate the priority queue.
    for test in filtered.candidates.values():
        valid_branches = test.branches_covered & total_branches - covered_set
        if valid_branches:
            marginal = len(valid_branches)
            dur = test.duration_s
            eff = marginal / dur if dur > 0 else (float("inf") if marginal > 0 else 0.0)

            node = _CandidateNode(
                test_id=test.test_id,
                branches=set(valid_branches),
                duration=dur,
                orig_branches=test.branches_covered,
                efficiency=eff,
                marginal=marginal,
                last_eval_cov_len=len(covered_set),
            )
            heapq.heappush(heap, node)

    target_count = (target_cov / 100.0) * len(total_branches)

    while heap and len(covered_set) < target_count:
        if len(total_branches) == len(covered_set):
            break

        node = heapq.heappop(heap)

        # Check if adding this test would exceed the time cap
        if elapsed_time + node.duration > time_cap:
            continue

        if node.last_eval_cov_len == len(covered_set):
            # Up to date, it's the best!
            selected_tests.append(
                SelectedTest(
                    test_id=node.test_id,
                    duration_s=node.duration,
                    branches_covered=len(node.orig_branches),
                    marginal_branches=node.marginal,
                    efficiency=node.efficiency,
                )
            )
            covered_set.update(node.orig_branches)
            elapsed_time += node.duration
        else:
            # Re-evaluate
            remaining_branches = node.branches - covered_set
            marginal = len(remaining_branches)
            if marginal > 0:
                eff = marginal / node.duration if node.duration > 0 else float("inf")
                node.branches = remaining_branches
                node.marginal = marginal
                node.efficiency = eff
                node.last_eval_cov_len = len(covered_set)
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
