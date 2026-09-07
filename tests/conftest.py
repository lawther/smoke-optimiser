import pytest

from smoke_optimiser.profiler.models import ImportGraph

pytest_plugins = ["pytester"]


@pytest.fixture
def empty_graph() -> ImportGraph:
    """An import graph that recorded nothing, for tests that are not about the graph."""
    return ImportGraph(
        edges=frozenset(),
        unattributed_modules=frozenset(),
        resolution_errors=0,
        error_samples=(),
    )
