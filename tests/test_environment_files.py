"""Tests for the environment-defining carve-out.

These paths refuse regardless of what the read map says about them, which is
the one rule in downwind selection that deliberately ignores a measurement.
Nothing opens a lockfile while the suite runs, so the map would report it as
depended on by nothing -- measured, and wrong.
"""

import pytest

from smoke_optimiser.downwind.environment_files import DEFAULT_ENVIRONMENT_FILES, is_environment_file


@pytest.mark.parametrize(
    "path",
    [
        "uv.lock",
        "poetry.lock",
        "pyproject.toml",
        "requirements.txt",
        "requirements-dev.txt",
        "Dockerfile",
        "Dockerfile.web",
        "docker-compose.yml",
        "docker-compose.prod.yaml",
        ".python-version",
    ],
)
def test_the_shipped_defaults_catch_what_moves_a_whole_suite(path: str) -> None:
    assert is_environment_file(path, DEFAULT_ENVIRONMENT_FILES)


def test_a_bare_filename_pattern_matches_at_any_depth() -> None:
    """A monorepo keeps its lockfile in a subdirectory, and it still moves everything."""
    assert is_environment_file("services/api/uv.lock", DEFAULT_ENVIRONMENT_FILES)


def test_a_pattern_with_a_directory_matches_only_there() -> None:
    assert is_environment_file("deploy/cluster.tf", ["deploy/*.tf"])
    assert not is_environment_file("src/cluster.tf", ["deploy/*.tf"])


@pytest.mark.parametrize("path", ["fixtures/rates.yaml", "README.md", "docs/guide.md", "src/app.py"])
def test_ordinary_files_are_left_to_the_maps(path: str) -> None:
    assert not is_environment_file(path, DEFAULT_ENVIRONMENT_FILES)
