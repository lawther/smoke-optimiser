"""The two roots, and what an offset means when there is nowhere to express it."""

import subprocess
from pathlib import Path

import pytest

from smoke_optimiser.downwind.changes import GitStatusError
from smoke_optimiser.paths import (
    ProjectPaths,
    offset_from,
    resolve_project_paths,
    resolve_project_paths_or_invocation_dir,
)
from smoke_optimiser.profiler.scope import WHOLE_REPOSITORY


def test_a_project_at_the_repository_root_has_no_offset() -> None:
    paths = ProjectPaths(repo_root=Path("/repo"), invocation_dir=Path("/repo"))

    assert paths.project_offset == WHOLE_REPOSITORY


def test_a_project_in_a_subdirectory_offsets_by_it() -> None:
    paths = ProjectPaths(repo_root=Path("/repo"), invocation_dir=Path("/repo/api"))

    assert paths.project_offset == "api"


def test_a_path_outside_the_repository_falls_back_to_the_root() -> None:
    # A pytest rootdir beyond the checkout cannot be expressed in the path space
    # at all. Reporting the root is what the tool did before the two roots were
    # told apart, and the configuration that produces it resolves no test root
    # inside the repository either -- so the scope check refuses the profile with
    # a message about the actual problem rather than about an offset nobody set.
    assert offset_from(Path("/repo"), Path("/elsewhere")) == WHOLE_REPOSITORY


def test_discovery_inside_a_repository_finds_the_top_level(tmp_path: Path) -> None:
    project = tmp_path / "api"
    project.mkdir()
    # git comes from PATH and the arguments are this test's own literals
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)  # noqa: S607

    paths = resolve_project_paths(project)

    assert paths.repo_root.resolve() == tmp_path.resolve()
    assert paths.project_offset == "api"


def test_discovery_outside_a_repository_raises(tmp_path: Path) -> None:
    # Raised rather than answered, so each caller states its own policy: downwind
    # has no diff to select from and must stop, while profiling only needs a root
    # to hang its paths on and can carry on.
    with pytest.raises(GitStatusError):
        resolve_project_paths(tmp_path)


def test_profiling_outside_a_repository_makes_the_invocation_directory_the_root(tmp_path: Path) -> None:
    paths = resolve_project_paths_or_invocation_dir(tmp_path)

    assert paths == ProjectPaths(repo_root=tmp_path, invocation_dir=tmp_path)
    assert paths.project_offset == WHOLE_REPOSITORY
