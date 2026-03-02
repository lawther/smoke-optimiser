from pathlib import Path

import pytest
from pydantic import ValidationError

from smoke_optimiser.reports.smoke_suite import SmokeSuiteFile, read_smoke_suite

SUPPORTED_VERSIONS: frozenset[int] = frozenset({1})


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


def _load_smoke_suite(config: pytest.Config) -> SmokeSuiteFile | None:
    """Load and validate the smoke suite file."""
    if not config.getoption("--smoke"):
        return None

    path_str = config.getoption("--smoke-file-path")
    path = Path(path_str)

    if not path.exists():
        pytest.exit(f"smoke-optimiser: smoke suite file not found: {path}", returncode=1)

    try:
        suite = read_smoke_suite(path)
    except (ValidationError, ValueError) as e:
        pytest.exit(f"smoke-optimiser: invalid smoke suite file: {path}: {e}", returncode=1)
    except Exception as e:
        pytest.exit(f"smoke-optimiser: error reading smoke suite file: {path}: {e}", returncode=1)

    if suite.version not in SUPPORTED_VERSIONS:
        pytest.exit(
            f"smoke-optimiser: unsupported smoke suite version {suite.version} "
            f"(supported: {sorted(SUPPORTED_VERSIONS)})",
            returncode=1,
        )

    return suite


def pytest_configure(config: pytest.Config) -> None:
    """Register the plugin and load the smoke suite."""
    if config.getoption("--smoke"):
        suite = _load_smoke_suite(config)
        # Store for later stages (Commit 3 will use StashKey)
        config._smoke_suite = suite
