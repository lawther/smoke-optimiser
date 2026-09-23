"""The two roots every path in this tool is relative to, told apart.

A single "project root" was enough only while the tool refused to run anywhere
but the repository root, which forced the two to coincide. They are different
questions:

* :attr:`ProjectPaths.repo_root` is the path space. git reports paths relative
  to the repository root whatever directory it ran in, so the profile's paths,
  the scope roots and the changed set are all expressed relative to it, and the
  maps agree with git by construction rather than by coincidence.
* :attr:`ProjectPaths.invocation_dir` is where the project is. Its
  pyproject.toml supplies ``[tool.smoke_optimiser]`` and
  ``[tool.coverage.run]``, the artefact paths resolve against it, and the
  profiled pytest subprocess is given it as its cwd -- so a project whose
  pyproject.toml is not at the repository root still gets its own ``testpaths``,
  ``pythonpath`` and rootdir, every one of which pytest resolves relative to
  where it finds that file.

Keeping both means a monorepo can keep its Python in a subdirectory, which is
the layout selecting a subset of the suite matters most in.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

from smoke_optimiser.downwind.changes import GitStatusError, repository_root
from smoke_optimiser.profiler.scope import WHOLE_REPOSITORY

if TYPE_CHECKING:
    from pathlib import Path


def offset_from(root: Path, path: Path) -> str:
    """Express ``path`` relative to ``root``, or :data:`WHOLE_REPOSITORY` if it is outside.

    Outside the repository means the answer cannot be expressed in the path
    space at all. Reporting the root itself is what the tool did before the two
    roots were told apart, and the configuration that produces it -- a pytest
    rootdir beyond the checkout -- already resolves no test root inside the
    repository, so the scope check refuses the profile with a message about the
    actual problem rather than about an offset nobody set.
    """
    try:
        relative = path.resolve().relative_to(root.resolve())
    except ValueError:
        return WHOLE_REPOSITORY
    return relative.as_posix()


class ProjectPaths(NamedTuple):
    """Where the repository is, and where within it this command was run.

    Attributes:
        repo_root: The repository's top level, and the root every path the
            profile stores is relative to.
        invocation_dir: The directory the command ran in, which holds the
            project's own pyproject.toml.
    """

    repo_root: Path
    invocation_dir: Path

    @property
    def project_offset(self) -> str:
        """Where the project sits inside the repository, as the profile records it.

        :data:`WHOLE_REPOSITORY` for the flat layout the two roots coincide in.
        """
        return offset_from(self.repo_root, self.invocation_dir)


def resolve_project_paths(invocation_dir: Path) -> ProjectPaths:
    """Discover both roots, or raise :class:`GitStatusError` saying git could not.

    Raises rather than quietly falling back, so each caller states its own
    policy: downwind selection cannot proceed without git, since there is no
    diff to select from, while profiling only needs somewhere to hang the paths
    and can carry on with the invocation directory.
    """
    return ProjectPaths(repo_root=repository_root(invocation_dir), invocation_dir=invocation_dir)


def resolve_project_paths_or_invocation_dir(invocation_dir: Path) -> ProjectPaths:
    """Both roots, falling back to a repository of one directory outside git.

    What profiling uses. A profile recorded outside a repository has no diff to
    be compared against and no ``git ls-files`` denominator, so its paths only
    need a root they are all consistent about -- and the invocation directory is
    the one the tool used before the two roots were told apart.
    """
    try:
        return resolve_project_paths(invocation_dir)
    except GitStatusError:
        return ProjectPaths(repo_root=invocation_dir, invocation_dir=invocation_dir)
