from smoke_optimiser.optimiser.filters import apply_filters
from smoke_optimiser.profiler.models import ProfilingOutcome


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
