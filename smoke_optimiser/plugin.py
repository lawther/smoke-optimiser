import warnings
from pathlib import Path

import pytest
from pydantic import ValidationError

from smoke_optimiser.reports.downwind_suite import (
    BlindSpotModel,
    BlindSpotReason,
    DownwindSuiteFile,
    read_downwind_suite,
)
from smoke_optimiser.reports.smoke_suite import SmokeSuiteFile, read_smoke_suite

SUPPORTED_VERSIONS: frozenset[int] = frozenset({1})
SUPPORTED_DOWNWIND_VERSIONS: frozenset[int] = frozenset({1})

# Cache for the loaded smoke suite
_smoke_suite_key = pytest.StashKey[SmokeSuiteFile]()

# Cache for the loaded downwind selection
_downwind_suite_key = pytest.StashKey[DownwindSuiteFile]()


def pytest_addoption(parser: pytest.Parser) -> None:
    """Add CLI options for the smoke suite plugin."""
    group = parser.getgroup("smoke-optimiser")
    group.addoption(
        "--smoke",
        action="store_true",
        default=False,
        help="Run only the tests in the smoke suite.",
    )
    group.addoption(
        "--smoke-file-path",
        action="store",
        default=".smoke_suite.json",
        type=str,
        help="Location of the smoke suite file.",
    )
    group.addoption(
        "--downwind",
        action="store_true",
        default=False,
        help="Run only the tests downwind of the changed files.",
    )
    group.addoption(
        "--downwind-file-path",
        action="store",
        default=".downwind.json",
        type=str,
        help="Location of the downwind selection file.",
    )


def _load_smoke_suite(config: pytest.Config) -> SmokeSuiteFile | None:
    """Load and validate the smoke suite file."""
    if not config.getoption("--smoke"):
        return None

    path_str = config.getoption("--smoke-file-path")
    path = Path(path_str)

    if not path.exists():
        pytest.exit(
            f"smoke-optimiser: ❌ Error: smoke suite file not found: {path}\n"
            "  Hint: Run `smoke-optimiser` first to generate it.",
            returncode=1,
        )

    try:
        suite = read_smoke_suite(path)
    except (ValidationError, ValueError) as e:
        pytest.exit(f"smoke-optimiser: ❌ Error: invalid smoke suite file: {path}: {e}", returncode=1)
    # A blind catch is deliberate: reading the suite must never surface a traceback through
    # pytest's collection, so anything unexpected is reported as a clean error instead.
    except Exception as e:  # noqa: BLE001
        pytest.exit(
            f"smoke-optimiser: ❌ Error: error reading smoke suite file: {path}: {e}",
            returncode=1,
        )

    if suite.version not in SUPPORTED_VERSIONS:
        pytest.exit(
            f"smoke-optimiser: ❌ Error: unsupported smoke suite version {suite.version} "
            f"(supported: {sorted(SUPPORTED_VERSIONS)})",
            returncode=1,
        )

    return suite


def _load_downwind_suite(config: pytest.Config) -> DownwindSuiteFile | None:
    """Load and validate the downwind selection file."""
    if not config.getoption("--downwind"):
        return None

    path_str = config.getoption("--downwind-file-path")
    path = Path(path_str)

    if not path.exists():
        pytest.exit(
            f"smoke-optimiser: ❌ Error: downwind selection file not found: {path}\n"
            "  Hint: Run `smoke-optimiser --downwind` first to generate it.",
            returncode=1,
        )

    try:
        suite = read_downwind_suite(path)
    except (ValidationError, ValueError) as e:
        pytest.exit(f"smoke-optimiser: ❌ Error: invalid downwind selection file: {path}: {e}", returncode=1)
    # A blind catch is deliberate: reading the suite must never surface a traceback through
    # pytest's collection, so anything unexpected is reported as a clean error instead.
    except Exception as e:  # noqa: BLE001
        pytest.exit(
            f"smoke-optimiser: ❌ Error: error reading downwind selection file: {path}: {e}",
            returncode=1,
        )

    if suite.version not in SUPPORTED_DOWNWIND_VERSIONS:
        pytest.exit(
            f"smoke-optimiser: ❌ Error: unsupported downwind selection version {suite.version} "
            f"(supported: {sorted(SUPPORTED_DOWNWIND_VERSIONS)})",
            returncode=1,
        )

    return suite


def pytest_configure(config: pytest.Config) -> None:
    """Register the plugin and load the smoke suite and/or downwind selection."""
    if config.getoption("--smoke"):
        suite = _load_smoke_suite(config)
        if suite:
            config.stash[_smoke_suite_key] = suite

    if config.getoption("--downwind"):
        downwind = _load_downwind_suite(config)
        if downwind:
            config.stash[_downwind_suite_key] = downwind


def _filter_to_smoke_suite(config: pytest.Config, suite: SmokeSuiteFile, items: list[pytest.Item]) -> None:
    """Deselect everything outside the smoke suite, and warn about missing tests."""
    smoke_test_ids = {t.test_id for t in suite.smoke_tests}
    selected = [item for item in items if item.nodeid in smoke_test_ids]
    deselected = [item for item in items if item.nodeid not in smoke_test_ids]

    if deselected:
        config.hook.pytest_deselected(items=deselected)
    items[:] = selected

    found_ids = {item.nodeid for item in selected}
    for test_id in sorted(smoke_test_ids - found_ids):
        warnings.warn(
            f"smoke-optimiser: ⚠️ Warning: smoke test not found in collection: {test_id}",
            stacklevel=2,
        )


def _filter_to_downwind_suite(config: pytest.Config, downwind: DownwindSuiteFile, items: list[pytest.Item]) -> None:
    """Deselect everything outside the downwind selection, and warn about missing tests.

    Skipped entirely when blind_spots is non-empty: the profile could not
    answer for something, the answer is the full suite, and node_ids is
    empty, so collection is left untouched.
    """
    if downwind.blind_spots:
        return

    downwind_test_ids = set(downwind.node_ids)
    selected = [item for item in items if item.nodeid in downwind_test_ids]
    deselected = [item for item in items if item.nodeid not in downwind_test_ids]

    if deselected:
        config.hook.pytest_deselected(items=deselected)
    items[:] = selected

    found_ids = {item.nodeid for item in selected}
    for test_id in sorted(downwind_test_ids - found_ids):
        warnings.warn(
            f"smoke-optimiser: ⚠️ Warning: downwind test not found in collection "
            f"(removed or renamed since profiling): {test_id}",
            stacklevel=2,
        )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Filter collected tests to only include those in the smoke suite or downwind selection."""
    if config.getoption("--smoke"):
        suite = config.stash.get(_smoke_suite_key, None)
        if suite:
            _filter_to_smoke_suite(config, suite, items)

    if config.getoption("--downwind"):
        downwind = config.stash.get(_downwind_suite_key, None)
        if downwind:
            _filter_to_downwind_suite(config, downwind, items)


def pytest_report_header(config: pytest.Config) -> list[str]:
    """Add smoke suite and/or downwind selection information to the report header."""
    lines: list[str] = []

    if config.getoption("--smoke"):
        suite = config.stash.get(_smoke_suite_key, None)
        if suite:
            path = config.getoption("--smoke-file-path")
            count = len(suite.smoke_tests)
            cov = suite.summary.smoke_coverage_pct
            raw_hostname = suite.machine.hostname or ""
            machine = raw_hostname[:4] + "***" if raw_hostname else "unknown machine"
            lines.append(
                f"smoke-optimiser: running smoke suite from {path} "
                f"({count} tests, {cov:.1f}% coverage, profiled on {machine})",
            )

    if config.getoption("--downwind"):
        downwind = config.stash.get(_downwind_suite_key, None)
        if downwind:
            path = config.getoption("--downwind-file-path")
            changed = len(downwind.changed_files)
            if downwind.blind_spots:
                reasons = ", ".join(_describe_blind_spot(b) for b in downwind.blind_spots)
                lines.append(
                    f"smoke-optimiser: downwind selection from {path} could not answer for "
                    f"{changed} changed files, running full suite ({reasons})",
                )
            else:
                count = len(downwind.node_ids)
                lines.append(
                    f"smoke-optimiser: running downwind suite from {path} "
                    f"({count} tests downwind of {changed} changed files)",
                )

    return lines


def _describe_blind_spot(blind_spot: BlindSpotModel) -> str:
    """Render one BlindSpotModel as a short human-readable clause."""
    if blind_spot.reason is BlindSpotReason.RESOLUTION_ERRORS:
        return f"{blind_spot.reason.value}: {blind_spot.resolution_errors}"
    return f"{blind_spot.reason.value}: {blind_spot.file}"
