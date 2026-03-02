from smoke_optimiser.optimiser.filters import apply_filters
from smoke_optimiser.profiler.models import ProfilingOutcome

EXPECTED_MATCH_COUNT = 2
UNMATCHED_INCLUDES_COUNT = 2


def _create_outcome(test_id: str, passed: bool = True, markers: list[str] | None = None) -> ProfilingOutcome:
    return ProfilingOutcome(
        test_id=test_id,
        duration_s=0.1,
        passed=passed,
        branches_covered=frozenset(),
        markers=frozenset(markers or []),
    )


def test_apply_filters_failed_always_excluded() -> None:
    tests = {
        "test_a": _create_outcome("test_a", passed=False),
        "test_b": _create_outcome("test_b", passed=True),
    }
    filtered = apply_filters(tests, [], [])
    assert "test_a" in filtered.failed
    assert "test_b" in filtered.candidates
    assert len(filtered.candidates) == 1


def test_apply_filters_glob_patterns() -> None:
    tests = {
        "tests/unit/test_a.py::test_1": _create_outcome("tests/unit/test_a.py::test_1"),
        "tests/integration/test_b.py::test_1": _create_outcome("tests/integration/test_b.py::test_1"),
    }
    filtered = apply_filters(tests, ["*unit*"], ["*integration*"])
    assert "tests/unit/test_a.py::test_1" in filtered.mandatory_included
    assert "tests/integration/test_b.py::test_1" in filtered.excluded


def test_apply_filters_marker_patterns() -> None:
    tests = {
        "test_smoke": _create_outcome("test_smoke", markers=["smoke"]),
        "test_slow": _create_outcome("test_slow", markers=["slow"]),
    }
    filtered = apply_filters(tests, ["@pytest.mark.smoke"], ["@pytest.mark.slow"])
    assert "test_smoke" in filtered.mandatory_included
    assert "test_slow" in filtered.excluded


def test_apply_filters_exclude_precedence() -> None:
    # If a test matches both, exclude wins
    tests = {
        "test_both": _create_outcome("test_both", markers=["smoke", "slow"]),
    }
    filtered = apply_filters(tests, ["@pytest.mark.smoke"], ["@pytest.mark.slow"])
    assert "test_both" in filtered.excluded
    assert "test_both" not in filtered.mandatory_included


def test_apply_filters_empty() -> None:
    tests = {"test_a": _create_outcome("test_a")}
    filtered = apply_filters(tests, [], [])
    assert "test_a" in filtered.candidates
    assert len(filtered.mandatory_included) == 0
    assert len(filtered.excluded) == 0
    assert len(filtered.failed) == 0


def test_apply_filters_unmatched() -> None:
    tests = {"test_a": _create_outcome("test_a")}
    filtered = apply_filters(tests, ["test_b", "@pytest.mark.none"], ["test_c"])
    assert "test_b" in filtered.unmatched_includes
    assert "@pytest.mark.none" in filtered.unmatched_includes
    assert "test_c" in filtered.unmatched_excludes
    assert len(filtered.unmatched_includes) == UNMATCHED_INCLUDES_COUNT
    assert len(filtered.unmatched_excludes) == 1


def test_apply_filters_file_path_match() -> None:
    """Verifies that naming a file matches all tests inside it."""
    tests = {
        "tests/test_greedy.py::test_1": _create_outcome("tests/test_greedy.py::test_1"),
        "tests/test_greedy.py::test_2": _create_outcome("tests/test_greedy.py::test_2"),
        "tests/test_other.py::test_1": _create_outcome("tests/test_other.py::test_1"),
    }
    # Match by file path prefix
    filtered = apply_filters(tests, ["tests/test_greedy.py"], [])
    assert "tests/test_greedy.py::test_1" in filtered.mandatory_included
    assert "tests/test_greedy.py::test_2" in filtered.mandatory_included
    assert "tests/test_other.py::test_1" in filtered.candidates
    assert len(filtered.mandatory_included) == EXPECTED_MATCH_COUNT


def test_apply_filters_test_name_match() -> None:
    """Verifies that naming just the test function matches it regardless of file path."""
    tests = {
        "tests/test_a.py::test_target": _create_outcome("tests/test_a.py::test_target"),
        "tests/test_b.py::test_target": _create_outcome("tests/test_b.py::test_target"),
        "tests/test_a.py::test_other": _create_outcome("tests/test_a.py::test_other"),
    }
    # Match by test name suffix
    filtered = apply_filters(tests, ["test_target"], [])
    assert "tests/test_a.py::test_target" in filtered.mandatory_included
    assert "tests/test_b.py::test_target" in filtered.mandatory_included
    assert "tests/test_a.py::test_other" in filtered.candidates
    assert len(filtered.mandatory_included) == EXPECTED_MATCH_COUNT
