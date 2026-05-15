import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field

from smoke_optimiser.environment import capture_environment
from smoke_optimiser.profiler.models import (
    ProfilingData,
    ProfilingMeta,
    ProfilingOutcome,
)


class CoverageMeta(BaseModel):
    """Metadata section of coverage.py JSON report."""

    version: str = "unknown"


class CoverageFile(BaseModel):
    """File section of coverage.py JSON report."""

    executed_branches: list[list[int]] = Field(default_factory=list)
    missing_branches: list[list[int]] = Field(default_factory=list)
    contexts: dict[str, list[str]] = Field(default_factory=dict)


class CoverageReport(BaseModel):
    """Full coverage.py JSON report structure."""

    meta: CoverageMeta = Field(default_factory=CoverageMeta)
    files: dict[str, CoverageFile] = Field(default_factory=dict)


class TestResolver:
    """Encapsulates test ID resolution logic and state."""

    def __init__(self, test_durations: dict[str, float]) -> None:
        self.test_durations = test_durations
        self.resolved_test_ids: dict[str, str | None] = {}
        # Pre-compute test names for faster lookup (fallback 3)
        self.candidate_names: dict[str, str] = {}
        for c in test_durations:
            clean_name = c.split("::")[-1].split("[")[0]
            if clean_name not in self.candidate_names:
                self.candidate_names[clean_name] = c

    def resolve(self, raw_test_id: str) -> str | None:
        """Normalise and resolve a raw test ID from coverage contexts."""
        if raw_test_id in self.resolved_test_ids:
            return self.resolved_test_ids[raw_test_id]

        # NORMALISE
        # Strip suffixes like "|run" or " (call)"
        clean_id = raw_test_id.split("|", maxsplit=1)[0].split(" (", maxsplit=1)[0]
        test_id = None

        # 1. Exact match
        if clean_id in self.test_durations:
            test_id = clean_id
        else:
            # 2. Suffix match
            for candidate in self.test_durations:
                if clean_id.endswith(candidate) or candidate.endswith(clean_id):
                    test_id = candidate
                    break

            # 3. Test name match (for dynamic_context = test_function which uses dot notation)
            if not test_id:
                clean_name = clean_id.split(".")[-1]
                if clean_name in self.candidate_names:
                    test_id = self.candidate_names[clean_name]

        self.resolved_test_ids[raw_test_id] = test_id
        return test_id


class CoverageCollector:
    """Stateful collector for coverage data during parsing."""

    def __init__(self) -> None:
        self.tests_lines: dict[str, dict[str, set[int]]] = {}
        self.tests_branches: dict[str, set[str]] = {}
        self.total_branches: set[str] = set()

    def add_line(self, test_id: str, file_path: str, line_num: int) -> None:
        """Record that a test executed a specific line."""
        if test_id not in self.tests_lines:
            self.tests_lines[test_id] = {}
        if file_path not in self.tests_lines[test_id]:
            self.tests_lines[test_id][file_path] = set()
        self.tests_lines[test_id][file_path].add(line_num)


def _map_contexts_to_lines(
    file_path: str,
    contexts: dict[str, list[str]],
    resolver: TestResolver,
    collector: CoverageCollector,
) -> None:
    """Map coverage contexts (tests) to the lines they executed in a file."""
    for line_str, context_list in contexts.items():
        try:
            line_num = int(line_str)
        except ValueError:
            continue

        for raw_test_id in context_list:
            if raw_test_id == "":
                continue

            test_id = resolver.resolve(raw_test_id)

            if test_id:
                collector.add_line(test_id, file_path, line_num)


def _infer_branch_coverage(
    file_path: str,
    executed_branches: list[list[int]],
    collector: CoverageCollector,
) -> None:
    """Infer branch coverage for a file based on executed lines."""
    # Pre-format branch strings to avoid redundant O(tests * branches) string concatenations
    formatted_branches = [(br[0], br[1], f"{file_path}:{br[0]}->{br[1]}") for br in executed_branches]

    for test_id, file_lines in collector.tests_lines.items():
        if file_path not in file_lines:
            continue
        lines = file_lines[file_path]

        if test_id not in collector.tests_branches:
            collector.tests_branches[test_id] = set()

        for u, v, branch_str in formatted_branches:
            # A test covers branch u->v if it hit line u AND (it hit line v OR v is an exit branch)
            if u in lines and (v <= 0 or v in lines):
                collector.tests_branches[test_id].add(branch_str)


def _assemble_profiling_data(
    collector: CoverageCollector,
    test_durations: dict[str, float],
    test_outcomes: dict[str, bool],
    test_markers: dict[str, frozenset[str]],
    coverage_version: str,
) -> ProfilingData:
    """Assemble final profiling results into a ProfilingData object."""
    profiling_outcomes = {}
    for test_id, duration in test_durations.items():
        branches = frozenset(collector.tests_branches.get(test_id, set()))

        profiling_outcomes[test_id] = ProfilingOutcome(
            test_id=test_id,
            duration_s=duration,
            passed=test_outcomes.get(test_id, False),
            branches_covered=branches,
            markers=test_markers.get(test_id, frozenset()),
        )

    meta = ProfilingMeta(
        timestamp=datetime.now(UTC),
        commit=None,
        python_version=sys.version,
        coverage_version=str(coverage_version),
        command=" ".join(sys.argv),
        machine=capture_environment(),
    )

    return ProfilingData(
        meta=meta,
        tests=profiling_outcomes,
        total_branches=frozenset(collector.total_branches),
    )


def parse_coverage_json(
    coverage_json_path: Path,
    test_durations: dict[str, float],
    test_outcomes: dict[str, bool],
    test_markers: dict[str, frozenset[str]],
) -> ProfilingData:
    """Parse coverage.py JSON output.

    Since coverage.py's JSON export maps contexts to line numbers rather than
    branches, we infer branch coverage: a test covers branch A->B if it
    executed both line A and line B (or if B is an exit branch <= 0).
    """
    collector = CoverageCollector()

    with open(coverage_json_path, "rb") as f:
        raw_data = json.load(f)
        full_data = CoverageReport.model_validate(raw_data)
        coverage_version = full_data.meta.version
        resolver = TestResolver(test_durations)

        for file_path, file_data in full_data.files.items():
            executed_branches = file_data.executed_branches
            missing_branches = file_data.missing_branches
            all_branches = executed_branches + missing_branches

            for br in all_branches:
                collector.total_branches.add(f"{file_path}:{br[0]}->{br[1]}")

            _map_contexts_to_lines(file_path, file_data.contexts, resolver, collector)
            _infer_branch_coverage(file_path, executed_branches, collector)

    return _assemble_profiling_data(
        collector,
        test_durations,
        test_outcomes,
        test_markers,
        coverage_version,
    )
