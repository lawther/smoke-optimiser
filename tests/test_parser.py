import json
from pathlib import Path

from smoke_optimiser.profiler.parser import parse_coverage_json

EXPECTED_TEST_COUNT = 2


def test_parse_coverage_json(tmp_path: Path) -> None:
    coverage_data = {
        "meta": {"version": "7.0"},
        "files": {
            "app.py": {
                "executed_branches": [[1, 2], [1, 3]],
                "missing_branches": [[5, 6]],
                "contexts": {
                    "tests/test_app.py::test_case_1": {"executed_branches": [[1, 2]]},
                    "tests/test_app.py::test_case_2": {"executed_branches": [[1, 3]]},
                },
            }
        },
    }
    coverage_file = tmp_path / "coverage.json"
    coverage_file.write_text(json.dumps(coverage_data))

    test_durations = {
        "tests/test_app.py::test_case_1": 0.1,
        "tests/test_app.py::test_case_2": 0.2,
    }
    test_outcomes = {
        "tests/test_app.py::test_case_1": True,
        "tests/test_app.py::test_case_2": True,
    }
    test_markers = {
        "tests/test_app.py::test_case_1": frozenset(["unit"]),
        "tests/test_app.py::test_case_2": frozenset(["unit"]),
    }

    profiling_data = parse_coverage_json(coverage_file, test_durations, test_outcomes, test_markers)

    assert profiling_data.meta.coverage_version == "7.0"
    assert len(profiling_data.tests) == EXPECTED_TEST_COUNT
    assert profiling_data.tests["tests/test_app.py::test_case_1"].branches_covered == frozenset(["app.py:1->2"])
    assert profiling_data.total_branches == frozenset(["app.py:1->2", "app.py:1->3", "app.py:5->6"])
