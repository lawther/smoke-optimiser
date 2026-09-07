"""The scope a profile was captured under: which files it could ever know about.

A profile's downwind maps are a fact about the codebase as it stood when the
suite ran. Deciding whether the codebase has since grown past them means
comparing the files that exist now against the files the maps know -- and that
comparison is only meaningful over the files a REGENERATED profile would know.
A ``.py`` file coverage never measures and pytest never collects is unknown to
the maps no matter how many times the profile is rebuilt, so counting it as
divergence would pin the profile to permanently expired.

The scope is therefore the coverage targets and the pytest test paths, as
actually resolved by the profiling run and recorded into the profile. Resolved
by the run rather than recomputed from configuration, because a project's own
``[tool.pytest.ini_options]`` ``addopts`` and ``testpaths`` are applied by pytest
itself and are invisible to the process that launched it: recomputation lets
"known to the maps" and "in scope" drift apart silently, which is the one
failure this module exists to prevent.

Coverage roots and test roots are kept apart because they answer the question
differently. Everything under a coverage root is measured, executed or not, so
every ``.py`` file beneath one is a file the profile would know. A test root is
only as precise as the project made it: ``testpaths = ["tests"]`` names a
directory whose every ``.py`` file is test code the maps would see, while a
project that configures nothing leaves pytest pointed at the repository root,
where "under the test root" says nothing at all. In that one case scope narrows
to the files pytest would actually collect -- otherwise a vendored package's
tests or a manual ``scripts/test_connection.py`` would be permanently unknown,
and the profile permanently expired.

:func:`resolve_scope` runs inside the profiled pytest process, where those
settings have already been applied. :func:`files_in_scope` applies the recorded
result. Both are pure; enumerating the tree is the caller's job.
"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

WHOLE_REPOSITORY = "."
"""Scope root covering every file in the repository.

What a bare ``--cov`` means, and what pytest's arguments come to when a project
configures neither ``testpaths`` nor a path on the command line.
"""

CONFTEST = "conftest.py"
"""Collected by pytest whatever ``python_files`` says, so it is always in scope."""


@dataclass(frozen=True)
class ProfileScope:
    """What a profiling run measured, as repository-relative roots.

    Attributes:
        coverage_roots: The resolved ``--cov`` targets. Coverage records every
            ``.py`` file beneath these whether or not a test executed it, so all
            of them are files the profile knows.
        test_roots: The paths pytest collected from, after ``testpaths`` and
            ``addopts`` were applied.
        test_file_patterns: pytest's resolved ``python_files``. Only consulted
            for a test root of the whole repository, where a root alone cannot
            distinguish project test code from anything else in the tree.
    """

    coverage_roots: frozenset[str]
    test_roots: frozenset[str]
    test_file_patterns: tuple[str, ...]

    @property
    def is_empty(self) -> bool:
        """Does this scope name nothing at all?

        A profile whose scope is empty can put no file in scope, so nothing ever
        looks diverged and it answers confidently forever. Callers treat it as
        unusable rather than as a scope that happens to match nothing.
        """
        return not self.coverage_roots and not self.test_roots

    @property
    def collects_from_whole_repository(self) -> bool:
        """Was pytest pointed at the repository root rather than a test directory?

        True means the project configured no ``testpaths`` and passed no path, so
        test code cannot be told apart from the rest of the tree by location.
        """
        return WHOLE_REPOSITORY in self.test_roots


def _repo_relative(path: Path, project_root: Path) -> str | None:
    """Express ``path`` relative to the repository, or None if it is outside it.

    Outside means site-packages and anything else beyond the checkout: git will
    never list such a file, so it can neither be in scope nor diverge from the
    maps.
    """
    try:
        relative = path.resolve().relative_to(project_root.resolve())
    except ValueError:
        return None
    return relative.as_posix()


def _package_directory(name: str) -> Path | None:
    """Where an importable package or module lives, as coverage.py would find it."""
    try:
        spec = importlib.util.find_spec(name)
    except (ImportError, ValueError, AttributeError, TypeError):
        return None
    if spec is None:
        return None
    if spec.submodule_search_locations:
        return Path(next(iter(spec.submodule_search_locations)))
    return Path(spec.origin) if spec.origin else None


def _resolve_cov_source(*, source: str | bool, invocation_dir: Path, project_root: Path) -> str | None:
    """Turn one ``--cov`` value into a repo-relative coverage root.

    coverage.py accepts either a path or an importable name, and resolves a name
    only when no such path exists, so this follows the same order. A name that
    resolves outside the repository is dropped. One that resolves to nothing --
    a typo, or a package the profiled environment cannot import -- is kept as
    written: it matches no tracked file, which is harmless, and is not worth
    failing an instrumented run that coverage itself was content to finish.
    """
    if source is True or source == "":
        return WHOLE_REPOSITORY

    as_path = invocation_dir / str(source)
    if as_path.exists():
        return _repo_relative(as_path, project_root)

    package = _package_directory(str(source))
    if package is not None:
        return _repo_relative(package, project_root)

    return str(source)


def _resolve_test_path(arg: str, invocation_dir: Path, project_root: Path) -> str | None:
    """Turn one pytest positional argument into a repo-relative test root.

    Arguments may name a directory, a file, or a single test by node id; the
    part before ``::`` is the file the maps would know about.
    """
    return _repo_relative(invocation_dir / arg.split("::", maxsplit=1)[0], project_root)


def resolve_scope(
    cov_sources: Sequence[str | bool],
    args: Sequence[str],
    test_file_patterns: Sequence[str],
    invocation_dir: Path,
    project_root: Path,
) -> ProfileScope:
    """The scope a profiling run measured, read from inside that run.

    Args:
        cov_sources: pytest-cov's resolved ``--cov`` values, which may include
            True for a bare ``--cov`` meaning no source filtering at all.
        args: pytest's own positional arguments, after ``testpaths`` and
            ``addopts`` have been applied.
        test_file_patterns: pytest's resolved ``python_files``.
        invocation_dir: The directory pytest was invoked from, which the
            arguments are relative to.
        project_root: The repository root the profile's paths are relative to.

    Test paths belong in the scope alongside the coverage targets. Under a
    typical ``--cov=mypackage`` the test modules are absent from the measured
    files and reach the maps only as modules the import tracer could not
    attribute -- so scoping to the coverage targets alone would leave a new test
    file invisible, and the maps would answer confidently and wrongly for the
    source it exercises.
    """
    coverage_roots = {
        _resolve_cov_source(source=source, invocation_dir=invocation_dir, project_root=project_root)
        for source in cov_sources
    }
    test_roots = {_resolve_test_path(arg, invocation_dir, project_root) for arg in args}
    return ProfileScope(
        coverage_roots=frozenset(root for root in coverage_roots if root is not None),
        test_roots=frozenset(root for root in test_roots if root is not None),
        test_file_patterns=tuple(test_file_patterns),
    )


def _under_root(path: str, root: str) -> bool:
    """Is ``path`` at or beneath ``root``? Both repo-relative posix paths."""
    if root == WHOLE_REPOSITORY:
        return True
    return path == root or path.startswith(f"{root}/")


def _would_be_collected(path: str, test_file_patterns: Sequence[str]) -> bool:
    """Would pytest collect this file as test code?"""
    name = path.rsplit("/", maxsplit=1)[-1]
    return name == CONFTEST or any(fnmatch(name, pattern) for pattern in test_file_patterns)


def _in_scope(path: str, scope: ProfileScope) -> bool:
    """Would a profile regenerated under ``scope`` know about ``path``?"""
    if not path.endswith(".py"):
        return False
    if any(_under_root(path, root) for root in scope.coverage_roots):
        return True
    return any(
        root != WHOLE_REPOSITORY or _would_be_collected(path, scope.test_file_patterns)
        for root in scope.test_roots
        if _under_root(path, root)
    )


def files_in_scope(paths: Iterable[str], scope: ProfileScope) -> frozenset[str]:
    """The files among ``paths`` that a regenerated profile would know about.

    The whole membership rule in one place: a caller that applied only part of
    it would compare the maps against files they could never contain -- every
    data file in the tree, or every script outside the measured roots -- and read
    a profile that is perfectly current as diverged.

    Paths must be repository-relative posix strings, as ``git ls-files`` and the
    profile's own paths both are.
    """
    return frozenset(path for path in paths if _in_scope(path, scope))
