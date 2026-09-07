"""The scope a profile was captured under, and what it puts in scope.

Everything here is about one question: would a profile regenerated right now
know about this file? A scope that says yes for a file no regeneration could
ever reach pins the profile to permanently expired; a scope that says no for a
file a regeneration WOULD reach lets a stale profile answer confidently.
"""

import importlib
from pathlib import Path

import pytest

from smoke_optimiser.profiler.scope import ProfileScope, files_in_scope, resolve_scope

DEFAULT_PATTERNS = ("test_*.py", "*_test.py")


def _scope(
    coverage_roots: frozenset[str] = frozenset(),
    test_roots: frozenset[str] = frozenset(),
) -> ProfileScope:
    return ProfileScope(
        coverage_roots=coverage_roots,
        test_roots=test_roots,
        test_file_patterns=DEFAULT_PATTERNS,
    )


def test_a_coverage_source_that_is_a_path_becomes_that_path(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()

    scope = resolve_scope(
        cov_sources=["src"],
        args=[],
        test_file_patterns=DEFAULT_PATTERNS,
        invocation_dir=tmp_path,
        project_root=tmp_path,
    )

    assert scope.coverage_roots == frozenset({"src"})


def test_a_coverage_source_that_is_a_package_name_resolves_to_its_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """coverage.py accepts an importable name, and a src layout is where that bites.

    Under `--cov=mypkg` with the package at src/mypkg, treating the string as a
    path would record a root matching no tracked file at all -- an empty scope,
    so nothing ever looks diverged and a stale profile answers forever.
    """
    package = tmp_path / "src" / "mypkg"
    package.mkdir(parents=True)
    (package / "__init__.py").touch()
    monkeypatch.syspath_prepend(str(tmp_path / "src"))
    importlib.invalidate_caches()

    scope = resolve_scope(
        cov_sources=["mypkg"],
        args=[],
        test_file_patterns=DEFAULT_PATTERNS,
        invocation_dir=tmp_path,
        project_root=tmp_path,
    )

    assert scope.coverage_roots == frozenset({"src/mypkg"})


def test_a_coverage_source_outside_the_repository_is_dropped(tmp_path: Path) -> None:
    """An installed package lives in site-packages, which git will never list."""
    scope = resolve_scope(
        cov_sources=["pytest"],
        args=[],
        test_file_patterns=DEFAULT_PATTERNS,
        invocation_dir=tmp_path,
        project_root=tmp_path,
    )

    assert scope.coverage_roots == frozenset()


def test_an_unresolvable_coverage_source_is_kept_as_written(tmp_path: Path) -> None:
    """A typo must not abort a long instrumented run coverage itself finished.

    Kept verbatim rather than dropped: it matches no tracked file either way, and
    a recorded root the user can read beats a scope that silently lost one.
    """
    scope = resolve_scope(
        cov_sources=["not_a_package_or_path"],
        args=[],
        test_file_patterns=DEFAULT_PATTERNS,
        invocation_dir=tmp_path,
        project_root=tmp_path,
    )

    assert scope.coverage_roots == frozenset({"not_a_package_or_path"})


@pytest.mark.parametrize("source", [True, ""])
def test_a_bare_coverage_flag_covers_the_whole_repository(
    source: str | bool,  # noqa: FBT001 - pytest-cov really does put True in the list for a bare --cov
    tmp_path: Path,
) -> None:
    """`--cov` and `--cov=` both mean no source filtering, so everything is measured."""
    scope = resolve_scope(
        cov_sources=[source],
        args=[],
        test_file_patterns=DEFAULT_PATTERNS,
        invocation_dir=tmp_path,
        project_root=tmp_path,
    )

    assert scope.coverage_roots == frozenset({"."})


def test_a_node_id_argument_contributes_the_file_it_names(tmp_path: Path) -> None:
    scope = resolve_scope(
        cov_sources=[],
        args=["tests/test_app.py::test_add"],
        test_file_patterns=DEFAULT_PATTERNS,
        invocation_dir=tmp_path,
        project_root=tmp_path,
    )

    assert scope.test_roots == frozenset({"tests/test_app.py"})


def test_arguments_are_resolved_against_the_invocation_directory(tmp_path: Path) -> None:
    """Pytest's arguments are relative to where it was invoked, not the repo root."""
    (tmp_path / "backend" / "tests").mkdir(parents=True)

    scope = resolve_scope(
        cov_sources=[],
        args=["tests"],
        test_file_patterns=DEFAULT_PATTERNS,
        invocation_dir=tmp_path / "backend",
        project_root=tmp_path,
    )

    assert scope.test_roots == frozenset({"backend/tests"})


def test_a_scope_naming_nothing_is_empty(tmp_path: Path) -> None:
    scope = resolve_scope(
        cov_sources=[],
        args=[],
        test_file_patterns=DEFAULT_PATTERNS,
        invocation_dir=tmp_path,
        project_root=tmp_path,
    )

    assert scope.is_empty


def test_a_python_file_under_a_coverage_root_is_in_scope() -> None:
    scope = _scope(coverage_roots=frozenset({"smoke_optimiser"}))

    assert files_in_scope(["smoke_optimiser/downwind/maps.py"], scope) == frozenset(
        {"smoke_optimiser/downwind/maps.py"},
    )


def test_a_non_python_file_is_never_in_scope() -> None:
    """Otherwise every profile is born expired on its own README.

    The maps are a Python-execution map, so a data file is absent from them no
    matter how current they are.
    """
    scope = _scope(coverage_roots=frozenset({"."}), test_roots=frozenset({"."}))

    assert files_in_scope(["README.md", "pyproject.toml", ".gitignore"], scope) == frozenset()


def test_a_python_file_outside_every_root_is_not_in_scope() -> None:
    """This repository's own scripts/branch_summary.py is the live example.

    Tracked Python that no regeneration would ever teach the maps about, because
    coverage does not measure it and pytest does not collect it.
    """
    scope = _scope(coverage_roots=frozenset({"smoke_optimiser"}), test_roots=frozenset({"tests"}))

    assert files_in_scope(["scripts/branch_summary.py"], scope) == frozenset()


def test_a_root_matches_directories_not_name_prefixes() -> None:
    """`tests` must not swallow `tests_helpers`, which is a different directory."""
    scope = _scope(test_roots=frozenset({"tests"}))

    assert files_in_scope(["tests_helpers/thing.py"], scope) == frozenset()


def test_a_precise_test_root_puts_every_python_file_beneath_it_in_scope() -> None:
    """Support modules count, and are why a precise test root is worth having.

    A teammate's new tests/helpers/factories.py is code a regenerated profile
    would see (a test imports it), so it must expire the profile. Applying
    pytest's collection patterns here would drop it and under-select silently.
    """
    scope = _scope(coverage_roots=frozenset({"src"}), test_roots=frozenset({"tests"}))

    assert files_in_scope(["tests/helpers/factories.py"], scope) == frozenset({"tests/helpers/factories.py"})


def test_a_whole_repository_test_root_is_narrowed_to_what_pytest_would_collect() -> None:
    """With no configured test paths, location says nothing, so naming has to.

    Counting every .py file in the tree would put a manual script or a vendored
    package's module permanently out of the maps' reach, and the profile would
    be expired on every run forever.
    """
    scope = _scope(coverage_roots=frozenset({"src"}), test_roots=frozenset({"."}))

    in_scope = files_in_scope(
        ["test_app.py", "vendor/thing_test.py", "conftest.py", "scripts/deploy.py", "docs/example.py"],
        scope,
    )

    assert in_scope == frozenset({"test_app.py", "vendor/thing_test.py", "conftest.py"})


def test_a_whole_repository_coverage_root_still_covers_every_python_file() -> None:
    """A bare --cov measures the whole tree, so every .py file under it is known.

    Only the TEST half of a whole-repository scope is narrowed: coverage records
    files it never executed, so a file under a coverage root is one a regenerated
    profile would know about whether or not anything imports it.
    """
    scope = _scope(coverage_roots=frozenset({"."}), test_roots=frozenset({"."}))

    assert files_in_scope(["scripts/deploy.py"], scope) == frozenset({"scripts/deploy.py"})
