"""Tests for the import graph capture.

The bug class throughout this module is a MISSING edge. A missing edge raises
nothing, produces a perfectly valid profile, and only shows up later as tests
that were not selected -- so most of these tests assert on presence, and the
ones that matter most are about imports that are easy to never see at all.
"""

import importlib
import sys
from collections.abc import Collection, Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from smoke_optimiser.profiler.import_tracer import (
    ImportGraphIngestError,
    ImportTracer,
    ancestors,
    edge_targets,
    is_project_file,
    merge_graphs,
    read_graph,
    resolve_absolute_name,
    write_graph,
)
from smoke_optimiser.profiler.models import ImportEdge, ImportGraph


def _module(name: str, file: str | None) -> ModuleType:
    """A stand-in for an entry in sys.modules."""
    module = ModuleType(name)
    if file is not None:
        module.__file__ = file
    # A namespace package or a builtin simply has no __file__ attribute.
    return module


def test_ancestors_names_every_package_an_import_executes() -> None:
    # Importing a.b.c runs a/__init__.py and a/b/__init__.py too, so a change to
    # either of those affects whoever imported a.b.c.
    assert ancestors("a.b.c") == ["a", "a.b", "a.b.c"]
    assert ancestors("a") == ["a"]


def test_an_absolute_import_resolves_to_itself() -> None:
    assert resolve_absolute_name("pkg.mod", {"__name__": "other"}, 0) == "pkg.mod"


def test_a_relative_import_resolves_against_the_importing_package() -> None:
    scope = {"__name__": "pkg.sub.mod", "__package__": "pkg.sub"}

    assert resolve_absolute_name("sibling", scope, 1) == "pkg.sub.sibling"
    assert resolve_absolute_name("cousin", scope, 2) == "pkg.cousin"


def test_a_bare_relative_import_resolves_to_the_package_itself() -> None:
    """`from . import x` names no module of its own -- the target is the package."""
    scope = {"__name__": "pkg.sub.mod", "__package__": "pkg.sub"}

    assert resolve_absolute_name("", scope, 1) == "pkg.sub"


def test_a_relative_import_falls_back_to_the_module_name_without_a_package() -> None:
    """__package__ can be absent; __name__ then has to stand in for it.

    A module that is not itself a package must have its final component dropped
    first, which is what the missing __path__ signals.
    """
    plain = {"__name__": "pkg.sub.mod"}
    assert resolve_absolute_name("sibling", plain, 1) == "pkg.sub.sibling"

    package = {"__name__": "pkg.sub", "__path__": ["/somewhere"]}
    assert resolve_absolute_name("child", package, 1) == "pkg.sub.child"


def test_a_from_import_depends_on_the_submodule_it_names() -> None:
    modules = {"pkg.mod": _module("pkg.mod", "/p/pkg/mod.py")}

    targets = edge_targets("pkg", {"__name__": "importer"}, ["mod"], 0, modules)

    assert "pkg.mod" in targets


def test_a_from_import_of_an_attribute_adds_no_dependency() -> None:
    """`from pkg import CONSTANT` depends on pkg, and on no module called pkg.CONSTANT."""
    targets = edge_targets("pkg", {"__name__": "importer"}, ["CONSTANT"], 0, {})

    assert targets == ["pkg"]


def test_dependencies_inside_the_project_root_are_not_project_files() -> None:
    root = Path("/project")

    assert is_project_file(Path("/project/pkg/mod.py"), root)
    assert not is_project_file(Path("/project/.venv/lib/site-packages/x.py"), root)
    assert not is_project_file(Path("/elsewhere/mod.py"), root)


class _ExplodingScope(dict[str, Any]):
    """A globals mapping that fails the way a hostile __getattr__ might."""

    def get(self, key: str, default: object = None) -> object:
        del key, default
        msg = "no globals for you"
        raise RuntimeError(msg)


def test_a_failure_while_recording_never_breaks_the_import() -> None:
    """The import must still happen, and the failure must be visible in the profile.

    Raising out of __import__ would take down the entire suite being profiled, so
    the failure is swallowed -- but a swallowed failure is a missing edge, which
    would silently under-select tests, so it is counted rather than ignored.
    """
    tracer = ImportTracer()
    tracer.install()
    try:
        # A non-empty mapping, since an empty globals is indistinguishable from none.
        scope = _ExplodingScope({"__name__": "importer"})
        module = tracer._traced_import("json", scope, None, (), 0)  # noqa: SLF001 - the wrapper is the unit under test
    finally:
        tracer.uninstall()

    assert module is sys.modules["json"]

    graph = tracer.snapshot(Path("/project"), {})
    assert graph.resolution_errors == 1
    assert "no globals for you" in graph.error_samples[0]


def test_snapshot_keeps_only_project_files_and_makes_them_relative() -> None:
    tracer = ImportTracer()
    tracer.record_edges("pkg.a", ["pkg.b", "requests"])
    modules = {
        "pkg.a": _module("pkg.a", "/project/pkg/a.py"),
        "pkg.b": _module("pkg.b", "/project/pkg/b.py"),
        "requests": _module("requests", "/project/.venv/lib/site-packages/requests/__init__.py"),
        "sys": _module("sys", None),
    }

    graph = tracer.snapshot(Path("/project"), modules)

    assert graph.edges == frozenset({ImportEdge(importer="pkg/a.py", imported="pkg/b.py")})


def test_a_project_file_nothing_imports_is_reported_as_unanswerable() -> None:
    """Never as 'nothing imports this' -- that would select no tests for a change to it.

    A pytest11 plugin belonging to the project is loaded before any hook of ours
    runs, so no mechanism ever sees it being imported.
    """
    tracer = ImportTracer()
    tracer.record_edges("pkg.a", ["pkg.b"])
    modules = {
        "pkg.a": _module("pkg.a", "/project/pkg/a.py"),
        "pkg.b": _module("pkg.b", "/project/pkg/b.py"),
        "pkg.plugin": _module("pkg.plugin", "/project/pkg/plugin.py"),
    }

    graph = tracer.snapshot(Path("/project"), modules)

    assert "pkg/plugin.py" in graph.unattributed_modules
    # pkg/b.py has an importer, so the graph can answer for it.
    assert "pkg/b.py" not in graph.unattributed_modules


def test_a_module_loaded_before_the_tracer_was_installed_is_unanswerable() -> None:
    """The case no import hook can ever see.

    pytest loads entry-point plugins before any hook of ours runs, so a pytest11
    plugin belonging to the project is imported while nothing is watching. Reading
    it as "nothing imports this" would select no tests at all for a change to it.
    """
    tracer = ImportTracer()
    tracer.install()
    tracer.uninstall()

    graph = tracer.snapshot(Path("/project"), {"pkg.plugin": _module("pkg.plugin", "/project/pkg/plugin.py")})

    assert graph.unattributed_modules == frozenset({"pkg/plugin.py"})


# ---------------------------------------------------------------------------
# The trap: an import satisfied from the sys.modules cache
# ---------------------------------------------------------------------------


@pytest.fixture
def three_modules(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A package where two modules both import the same leaf."""
    pkg = tmp_path / "cachepkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "leaf.py").write_text("VALUE = 1\n")
    (pkg / "b.py").write_text("from cachepkg import leaf\n")
    (pkg / "c.py").write_text("from cachepkg import leaf\n")
    (pkg / "viaimportlib.py").write_text("import importlib\nmod = importlib.import_module('cachepkg.leaf')\n")

    monkeypatch.syspath_prepend(str(tmp_path))
    for name in [n for n in sys.modules if n.startswith("cachepkg")]:
        del sys.modules[name]

    yield tmp_path

    for name in [n for n in sys.modules if n.startswith("cachepkg")]:
        del sys.modules[name]


def test_the_second_importer_of_a_cached_module_is_recorded(three_modules: Path) -> None:
    """The whole reason builtins.__import__ is wrapped rather than sys.meta_path.

    Once cachepkg.leaf is in sys.modules, no finder and no sys.modules delta will
    ever fire for it again -- so c's dependency on leaf would be invisible, and a
    change to leaf would select b's tests but not c's. Which of b and c is seen
    would depend purely on collection order.
    """
    tracer = ImportTracer()
    tracer.install()
    try:
        importlib.import_module("cachepkg.b")
        importlib.import_module("cachepkg.c")
    finally:
        tracer.uninstall()

    graph = tracer.snapshot(three_modules, sys.modules)

    assert ImportEdge(importer="cachepkg/b.py", imported="cachepkg/leaf.py") in graph.edges
    assert ImportEdge(importer="cachepkg/c.py", imported="cachepkg/leaf.py") in graph.edges


def test_an_import_module_call_is_attributed_to_its_caller(three_modules: Path) -> None:
    """importlib.import_module bypasses builtins.__import__ entirely."""
    tracer = ImportTracer()
    tracer.install()
    try:
        importlib.import_module("cachepkg.viaimportlib")
    finally:
        tracer.uninstall()

    graph = tracer.snapshot(three_modules, sys.modules)

    assert ImportEdge(importer="cachepkg/viaimportlib.py", imported="cachepkg/leaf.py") in graph.edges


def test_importing_a_submodule_also_depends_on_its_packages(three_modules: Path) -> None:
    tracer = ImportTracer()
    tracer.install()
    try:
        importlib.import_module("cachepkg.b")
    finally:
        tracer.uninstall()

    graph = tracer.snapshot(three_modules, sys.modules)

    assert ImportEdge(importer="cachepkg/b.py", imported="cachepkg/__init__.py") in graph.edges


# ---------------------------------------------------------------------------
# Merging and persistence
# ---------------------------------------------------------------------------


def _graph(
    edges: set[ImportEdge],
    unattributed: Collection[str] = frozenset(),
    errors: int = 0,
    samples: tuple[str, ...] = (),
) -> ImportGraph:
    return ImportGraph(
        edges=frozenset(edges),
        unattributed_modules=frozenset(unattributed),
        resolution_errors=errors,
        error_samples=samples,
    )


EXPECTED_MERGED_ERRORS = 3


def test_merging_unions_what_each_worker_saw() -> None:
    a = _graph({ImportEdge("t1.py", "src.py")}, errors=1, samples=("boom",))
    b = _graph({ImportEdge("t2.py", "src.py")}, errors=2, samples=("bang",))

    merged = merge_graphs([a, b])

    assert merged.edges == frozenset({ImportEdge("t1.py", "src.py"), ImportEdge("t2.py", "src.py")})
    assert merged.resolution_errors == EXPECTED_MERGED_ERRORS
    assert set(merged.error_samples) == {"boom", "bang"}


def test_a_module_another_worker_attributed_stops_being_unanswerable() -> None:
    """Each xdist worker sees only the imports its own slice of the suite provoked.

    A worker that never ran the test importing src.py has no edge for it, and would
    call it unanswerable on its own. The run as a whole can answer for it.
    """
    a = _graph(set(), unattributed={"src.py"})
    b = _graph({ImportEdge("t2.py", "src.py")})

    merged = merge_graphs([a, b])

    assert merged.unattributed_modules == frozenset()


def test_a_graph_survives_a_write_and_read(tmp_path: Path) -> None:
    graph = _graph({ImportEdge("t.py", "src.py")}, unattributed={"conftest.py"}, errors=2, samples=("boom",))
    path = tmp_path / "graph.json"

    write_graph(graph, path)

    assert read_graph(path) == graph


def test_an_unreadable_graph_is_an_error_not_an_empty_graph(tmp_path: Path) -> None:
    """An empty graph reads as 'nothing imports anything', which selects nothing."""
    path = tmp_path / "graph.json"
    path.write_text('{"edges": "not a list"}')

    with pytest.raises(ImportGraphIngestError):
        read_graph(path)


def test_a_written_graph_is_ordered(tmp_path: Path) -> None:
    """Two identical runs should not produce byte-different files (see so-uup)."""
    graph = _graph({ImportEdge("b.py", "x.py"), ImportEdge("a.py", "x.py")}, unattributed={"z.py", "y.py"})
    first = tmp_path / "one.json"
    second = tmp_path / "two.json"

    write_graph(graph, first)
    write_graph(graph, second)

    assert first.read_text() == second.read_text()
    assert first.read_text().index('"a.py"') < first.read_text().index('"b.py"')


def test_installing_twice_still_restores_the_original_import() -> None:
    original = __import__
    tracer = ImportTracer()

    tracer.install()
    tracer.install()
    tracer.uninstall()

    assert __import__ is original


def test_an_uninstalled_tracer_records_nothing_further(three_modules: Path) -> None:
    tracer = ImportTracer()
    tracer.install()
    tracer.uninstall()

    importlib.import_module("cachepkg.b")

    graph = tracer.snapshot(three_modules, sys.modules)
    assert graph.edges == frozenset()
