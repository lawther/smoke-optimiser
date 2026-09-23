"""Record the real import graph of a test suite while it runs.

Coverage answers which tests EXECUTED a file. It cannot answer which tests
DEPEND on a file that only ever runs at import time -- a constants module, a
Pydantic model, a package re-export. Those files are executed during collection,
under no test context, so a change to one of them forces a full-suite run for
want of any relation tying it to a test.

The import graph is that relation. This module captures it by wrapping the two
functions every import goes through, because neither alone sees them all:

* ``builtins.__import__`` fires on every execution of an import statement, cache
  hits included, and hands us the importing module's globals. This is the only
  mechanism that sees the SECOND module to import something already loaded, and
  so the only one that yields a graph rather than a collection-order-dependent
  spanning tree. A ``sys.meta_path`` finder or a ``sys.modules`` delta would give
  the spanning tree, silently.
* ``importlib.import_module`` bypasses ``builtins.__import__`` entirely. It is
  wrapped separately, taking the importer from its caller's frame.

Neither can see an import made before the tracer was installed -- pytest loads
entry-point plugins before any hook runs, so a plugin belonging to the project is
always such a case. ``sys.modules`` at the end of the run is the authority on what
was loaded, and any project module in it that nothing was seen to import is
reported as unattributable rather than as unimported.

Recording an edge must never break the suite being profiled, so failures are
swallowed and counted. A swallowed failure is a MISSING edge, which would
under-select tests later, so the count travels in the profile.
"""

from __future__ import annotations

import builtins
import importlib
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from pydantic import ValidationError

from smoke_optimiser.profiler.models import ImportEdge, ImportEdgeModel, ImportGraph, ImportGraphModel

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence
    from types import ModuleType

MAX_ERROR_SAMPLES = 5

# Directories whose contents are dependencies rather than project code, even when
# they sit inside the project root.
EXCLUDED_PATH_PARTS = frozenset({".venv", "site-packages", "dist-packages"})


def ancestors(name: str) -> list[str]:
    """Every module name that must be imported for ``name`` to be imported.

    ``import a.b.c`` executes ``a/__init__.py`` and ``a/b/__init__.py`` as well,
    so a change to either affects whoever imported ``a.b.c``.
    """
    parts = name.split(".")
    return [".".join(parts[: i + 1]) for i in range(len(parts))]


def resolve_absolute_name(name: str, globals_: Mapping[str, Any], level: int) -> str:
    """Resolve the module name an import statement refers to.

    ``level`` is the number of leading dots: 0 for an absolute import, 1 for
    ``from . import x``, and so on. Relative names resolve against the importing
    module's ``__package__``, falling back to its ``__name__`` -- trimmed of its
    last component unless the module is itself a package, which is what having a
    ``__path__`` means.
    """
    if level == 0:
        return name

    package = globals_.get("__package__")
    if package is None:
        package = globals_.get("__name__", "")
        if "__path__" not in globals_:
            package = package.rpartition(".")[0]

    base = package.rsplit(".", level - 1)[0] if level > 1 else package
    return f"{base}.{name}" if name else base


def edge_targets(
    name: str,
    globals_: Mapping[str, Any],
    fromlist: Sequence[str] | None,
    level: int,
    modules: Mapping[str, ModuleType],
) -> list[str]:
    """Every module name one import statement makes the importer depend on."""
    full = resolve_absolute_name(name, globals_, level)
    if not full:
        return []

    targets = ancestors(full)

    # ``from pkg import mod`` depends on pkg.mod when that name is a submodule,
    # and on nothing extra when it is an attribute defined in pkg itself.
    targets.extend(f"{full}.{item}" for item in fromlist or () if f"{full}.{item}" in modules)

    return targets


def is_project_file(path: Path, repo_root: Path) -> bool:
    """Is this a file belonging to the project, rather than to a dependency?"""
    if not path.is_relative_to(repo_root):
        return False
    return not any(part in EXCLUDED_PATH_PARTS for part in path.parts)


class ImportTracer:
    """Captures import edges for the duration of a profiling run."""

    def __init__(self) -> None:
        self._edges: set[ImportEdge] = set()
        self._errors = 0
        self._error_samples: list[str] = []
        self._real_import = builtins.__import__
        self._real_import_module = importlib.import_module
        self._installed = False

    def _record_failure(self, exc: Exception) -> None:
        self._errors += 1
        if len(self._error_samples) < MAX_ERROR_SAMPLES:
            self._error_samples.append(repr(exc))

    def record_edges(self, importer: str | None, targets: Sequence[str]) -> None:
        """Record that ``importer`` depends on each of ``targets``."""
        if not importer:
            return
        self._edges.update(ImportEdge(importer=importer, imported=t) for t in targets if t != importer)

    def _traced_import(
        self,
        name: str,
        globals_: dict[str, Any] | None = None,
        locals_: dict[str, Any] | None = None,
        fromlist: Sequence[str] = (),
        level: int = 0,
    ) -> ModuleType:
        module = self._real_import(name, globals_, locals_, fromlist, level)
        try:
            scope = globals_ or {}
            self.record_edges(scope.get("__name__"), edge_targets(name, scope, fromlist, level, sys.modules))
        # A blind catch is deliberate and load-bearing: this runs inside every import
        # statement of the suite being profiled, so letting anything escape would take
        # down the user's whole test run. The count is surfaced in the profile because
        # a swallowed failure is a missing edge, and a missing edge under-selects.
        except Exception as exc:  # noqa: BLE001
            self._record_failure(exc)
        return module

    def _traced_import_module(self, name: str, package: str | None = None) -> ModuleType:
        module = self._real_import_module(name, package)
        try:
            # import_module gets no globals, but its caller's frame is unambiguous:
            # frame 0 is this wrapper, frame 1 is whoever called import_module.
            caller_globals = sys._getframe(1).f_globals  # noqa: SLF001 - the only way to see the calling module
            level = len(name) - len(name.lstrip("."))
            stripped = name.lstrip(".")
            # An explicit package argument says what a relative name resolves against,
            # overriding the caller's own __package__ -- but the importer is still the
            # caller, not the package it named.
            scope = {"__package__": package, "__path__": ()} if level and package else caller_globals
            self.record_edges(caller_globals.get("__name__"), edge_targets(stripped, scope, (), level, sys.modules))
        except Exception as exc:  # noqa: BLE001 - see _traced_import
            self._record_failure(exc)
        return module

    def install(self) -> None:
        """Start recording. Safe to call once; a second call is a no-op."""
        if self._installed:
            return
        # Replacing a builtin is exactly what this module is for, and the type checker
        # models each function's identity, so the casts state that these callables stand
        # in for the originals rather than match their declared identity.
        builtins.__import__ = cast("Any", self._traced_import)
        importlib.import_module = cast("Any", self._traced_import_module)
        self._installed = True

    def uninstall(self) -> None:
        """Stop recording, restoring whatever was in place before."""
        if not self._installed:
            return
        builtins.__import__ = cast("Any", self._real_import)
        importlib.import_module = cast("Any", self._real_import_module)
        self._installed = False

    def snapshot(self, repo_root: Path, modules: Mapping[str, ModuleType]) -> ImportGraph:
        """Resolve the recorded module names to project files.

        Module names are resolved to files only now, at the end of the run, because
        a module has to have been imported for ``__file__`` to exist. Anything
        outside the project -- the standard library, installed dependencies -- is
        dropped: a change there is not an edit this tool can select tests for.
        """
        root = repo_root.resolve()
        files: dict[str, str] = {}
        for module_name, module in list(modules.items()):
            file_attr = getattr(module, "__file__", None)
            if not file_attr:
                continue
            path = Path(file_attr).resolve()
            if is_project_file(path, root):
                files[module_name] = str(path.relative_to(root))

        edges = frozenset(
            ImportEdge(importer=files[edge.importer], imported=files[edge.imported])
            for edge in self._edges
            if edge.importer in files and edge.imported in files and files[edge.importer] != files[edge.imported]
        )

        # Every project module that was loaded but that nothing was seen to import.
        # sys.modules is the authority on what was loaded: a module imported BEFORE
        # the tracer was installed is in it, and was seen by neither wrapper. A
        # pytest11 plugin belonging to the project is exactly that, since pytest
        # loads entry-point plugins before any hook of ours runs. Reading those as
        # "nothing imports this" would select no tests at all for a change to them,
        # so they are declared unanswerable instead.
        with_importers = {edge.imported for edge in edges}
        unattributed = frozenset(file for file in files.values() if file not in with_importers)

        return ImportGraph(
            edges=edges,
            unattributed_modules=unattributed,
            resolution_errors=self._errors,
            error_samples=tuple(self._error_samples),
        )


class ImportGraphIngestError(RuntimeError):
    """Raised when an import graph written by the profiling hook cannot be read."""


def write_graph(graph: ImportGraph, path: Path) -> None:
    """Write one process's captured graph, in a stable order."""
    model = ImportGraphModel(
        edges=[ImportEdgeModel(importer=edge.importer, imported=edge.imported) for edge in sorted(graph.edges)],
        unattributed_modules=sorted(graph.unattributed_modules),
        resolution_errors=graph.resolution_errors,
        error_samples=list(graph.error_samples),
    )
    path.write_text(model.model_dump_json())


def read_graph(path: Path) -> ImportGraph:
    """Read back one process's captured graph."""
    try:
        return ImportGraphModel.model_validate_json(path.read_bytes()).to_import_graph()
    except (OSError, ValidationError) as exc:
        msg = (
            f"could not read the import graph written by the profiling hook ({path}): {exc}. "
            "A partial graph would look exactly like a project whose files import less than "
            "they do, which under-selects tests rather than failing."
        )
        raise ImportGraphIngestError(msg) from exc


def merge_graphs(graphs: Iterable[ImportGraph]) -> ImportGraph:
    """Combine the graphs several processes captured of the same run.

    Under pytest-xdist each worker sees only the imports its own slice of the suite
    provoked, so the run's graph is the union of theirs. A module one worker could
    not attribute may well have been attributed by another, so those are resolved
    against the merged edges rather than simply unioned.
    """
    edges: set[ImportEdge] = set()
    loaded: set[str] = set()
    errors = 0
    samples: list[str] = []

    for graph in graphs:
        edges |= graph.edges
        loaded |= graph.unattributed_modules
        errors += graph.resolution_errors
        samples.extend(graph.error_samples)

    with_importers = {edge.imported for edge in edges}

    return ImportGraph(
        edges=frozenset(edges),
        unattributed_modules=frozenset(loaded - with_importers),
        resolution_errors=errors,
        error_samples=tuple(samples[:MAX_ERROR_SAMPLES]),
    )
