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
    tests_branches: dict[str, set[str]] = {}
    total_branches: set[str] = set()
    coverage_version = "unknown"

    with open(coverage_json_path, "rb") as f:
        full_data = json.load(f)
        coverage_version = full_data.get("meta", {}).get("version", "unknown")

        test_durations_keys = set(test_durations.keys())
        raw_to_test_id = {}

        test_durations_names = {cand.split("::")[-1].split("[")[0]: cand for cand in test_durations_keys}

        for file_path, file_data in full_data.get("files", {}).items():
            executed_branches = file_data.get("executed_branches", [])
            missing_branches = file_data.get("missing_branches", [])
            all_branches = executed_branches + missing_branches

            for br in all_branches:
                total_branches.add(f"{file_path}:{br[0]}->{br[1]}")

            contexts = file_data.get("contexts", {})

            # Local mapping for this file: test_id -> set of lines executed
            test_lines_in_file = {}

            for line_str, context_list in contexts.items():
                try:
                    line_num = int(line_str)
                except ValueError:
                    continue

                for raw_test_id in context_list:
                    if not raw_test_id:
                        continue

                    # NORMALIZE
                    test_id = raw_to_test_id.get(raw_test_id)
                    if test_id is None and raw_test_id not in raw_to_test_id:
                        # Strip suffixes like "|run" or " (call)"
                        clean_id = raw_test_id.split("|", 1)[0].split(" (", 1)[0]

                        # 1. Exact match
                        if clean_id in test_durations_keys:
                            test_id = clean_id
                        else:
                            # 2. Suffix match
                            for candidate in test_durations_keys:
                                if clean_id.endswith(candidate) or candidate.endswith(clean_id):
                                    test_id = candidate
                                    break

                            # 3. Test name match (for dynamic_context = test_function which uses dot notation)
                            if not test_id:
                                clean_name = clean_id.split(".")[-1]
                                if clean_name in test_durations_names:
                                    test_id = test_durations_names[clean_name]

                        raw_to_test_id[raw_test_id] = test_id

                    if test_id:
                        try:
                            test_lines_in_file[test_id].add(line_num)
                        except KeyError:
                            test_lines_in_file[test_id] = {line_num}

            if executed_branches and test_lines_in_file:
                # Group tests by exactly the lines they executed in this file
                # so we only evaluate `u in lines` once per group of tests that have the same lines
                lines_to_tests = {}
                for test_id, lines_set in test_lines_in_file.items():
                    lines_frozenset = frozenset(lines_set)
                    try:
                        lines_to_tests[lines_frozenset].append(test_id)
                    except KeyError:
                        lines_to_tests[lines_frozenset] = [test_id]

                for lines_frozenset, test_ids in lines_to_tests.items():
                    branches_covered_by_group = []
                    for br in executed_branches:
                        u, v = br[0], br[1]
                        if u in lines_frozenset and (v <= 0 or v in lines_frozenset):
                            branches_covered_by_group.append(f"{file_path}:{u}->{v}")

                    if branches_covered_by_group:
                        for test_id in test_ids:
                            try:
                                tb = tests_branches[test_id]
                            except KeyError:
                                tb = tests_branches[test_id] = set()
                            tb.update(branches_covered_by_group)

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
