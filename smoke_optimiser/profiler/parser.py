import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from smoke_optimiser.environment import capture_environment
from smoke_optimiser.profiler.models import (
    ProfilingData,
    ProfilingMeta,
    ProfilingOutcome,
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
    tests_lines: dict[str, dict[str, set[int]]] = {}
    tests_branches: dict[str, set[str]] = {}
    total_branches: set[str] = set()
    coverage_version = "unknown"

    with open(coverage_json_path, "rb") as f:
        full_data = json.load(f)
        coverage_version = full_data.get("meta", {}).get("version", "unknown")

        for file_path, file_data in full_data.get("files", {}).items():
            executed_branches = file_data.get("executed_branches", [])
            missing_branches = file_data.get("missing_branches", [])
            all_branches = executed_branches + missing_branches

            for br in all_branches:
                total_branches.add(f"{file_path}:{br[0]}->{br[1]}")

            contexts = file_data.get("contexts", {})
            for line_str, context_list in contexts.items():
                try:
                    line_num = int(line_str)
                except ValueError:
                    continue

                for raw_test_id in context_list:
                    if raw_test_id == "":
                        continue

                    # NORMALIZE
                    # Strip suffixes like "|run" or " (call)"
                    clean_id = raw_test_id.split("|")[0].split(" (")[0]
                    test_id = None

                    # 1. Exact match
                    if clean_id in test_durations:
                        test_id = clean_id
                    else:
                        # 2. Suffix match
                        for candidate in test_durations:
                            if clean_id.endswith(candidate) or candidate.endswith(clean_id):
                                test_id = candidate
                                break

                        # 3. Test name match (for dynamic_context = test_function which uses dot notation)
                        # e.g. tests.test_app.test_add_negative vs tests/test_app.py::test_add_negative
                        if not test_id:
                            clean_name = clean_id.split(".")[-1]
                            for candidate in test_durations:
                                candidate_name = candidate.split("::")[-1].split("[")[0]  # handle parametrization
                                if clean_name == candidate_name:
                                    test_id = candidate
                                    break

                    if test_id:
                        if test_id not in tests_lines:
                            tests_lines[test_id] = {}
                        if file_path not in tests_lines[test_id]:
                            tests_lines[test_id][file_path] = set()
                        tests_lines[test_id][file_path].add(line_num)

            # Now infer branch coverage for this file
            for test_id, file_lines in tests_lines.items():
                if file_path not in file_lines:
                    continue
                lines = file_lines[file_path]

                if test_id not in tests_branches:
                    tests_branches[test_id] = set()

                for br in executed_branches:
                    u, v = br[0], br[1]
                    # A test covers branch u->v if it hit line u AND (it hit line v OR v is an exit branch)
                    if u in lines and (v <= 0 or v in lines):
                        tests_branches[test_id].add(f"{file_path}:{u}->{v}")

    profiling_outcomes = {}
    for test_id, duration in test_durations.items():
        branches = frozenset(tests_branches.get(test_id, set()))

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
        total_branches=frozenset(total_branches),
    )
