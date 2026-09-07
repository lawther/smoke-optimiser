from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, NamedTuple

from pydantic import BaseModel, Field

from smoke_optimiser.environment import MachineEnvironment

PROFILE_SCHEMA_VERSION = 1
"""Schema version this build writes to a profiling data file.

Bumped whenever ProfilingDataFile's shape changes in a way that makes an
older profile unreadable. A profile is a cache one instrumented run
rebuilds, so there is no migration path -- a mismatch just tells the user
to reprofile.
"""


@dataclass(frozen=True)
class ProfilingOutcome:
    """Individual test outcome with duration and coverage.

    Attributes:
        files_covered: Project-relative paths of every measured file this test
            executed, whether or not that file contains any branch. A file of
            straight-line code -- constants, re-exports, model declarations --
            contributes no branch ids at all, so ``branches_covered`` cannot
            answer which tests touch it.
    """

    test_id: str
    duration_s: float
    passed: bool
    branches_covered: frozenset[str]
    files_covered: frozenset[str]
    markers: frozenset[str]


class ImportEdge(NamedTuple):
    """One module depending on another, as project-relative file paths."""

    importer: str
    imported: str


@dataclass(frozen=True)
class ImportGraph:
    """Which project files import which, captured while the suite ran.

    Coverage says which tests executed a file. This says which files DEPEND on a
    file, which is the only thing that can answer for a module that runs solely at
    import time -- constants, model declarations, package re-exports. Without it a
    change to such a file has no relation to any test and forces a full run.

    Attributes:
        unattributed_modules: Project files observed being loaded that nothing was
            seen to import. Test modules and conftest files belong here legitimately
            (pytest loads them, no module imports them), as does anything loaded
            through machinery the tracer cannot attribute. A change to one of these
            means the graph CANNOT ANSWER, which must fall back to running
            everything -- reading it as "nothing imports this" would select no tests
            at all.
        resolution_errors: How many times recording an edge failed and was
            swallowed. Recording cannot be allowed to break the suite being
            profiled, but each swallowed failure is a MISSING edge, and a missing
            edge silently under-selects, so a non-zero count means the graph is
            incomplete by an unknown amount.
        error_samples: The first few of those failures, for diagnosis.
    """

    edges: frozenset[ImportEdge]
    unattributed_modules: frozenset[str]
    resolution_errors: int
    error_samples: tuple[str, ...]


@dataclass(frozen=True)
class ProfilingMeta:
    """Metadata for a profiling run.

    Attributes:
        xdist_workers: Number of pytest-xdist workers the suite ran under, 1 for
            a serial run. Above 1, every duration was measured while that many
            tests competed for the machine, so the durations are not comparable
            with each other or with a serial profile. The coverage map is
            unaffected.
    """

    timestamp: datetime
    commit: str | None
    python_version: str
    coverage_version: str
    command: str
    machine: MachineEnvironment
    xdist_workers: int


@dataclass(frozen=True)
class ProfilingData:
    """Complete profiling data for a test suite.

    Attributes:
        measured_files: Project-relative paths of every file coverage measured,
            including files no test executed. A changed file that is absent
            here is one coverage never saw at all, which is a different thing
            from a file no test happens to run.
        unattributable_branches: Branches that were executed, but only outside
            any test context -- module-level code running at import time. No
            selection of tests can ever cover them, so they cap the coverage
            the optimiser can reach.
    """

    meta: ProfilingMeta
    tests: dict[str, ProfilingOutcome]
    total_branches: frozenset[str]
    measured_files: frozenset[str]
    import_graph: ImportGraph
    unattributable_branches: frozenset[str] = frozenset()


@dataclass(frozen=True)
class SuiteRunResults:
    """Per-test facts gathered by the pytest hook, keyed by node id."""

    durations: dict[str, float]
    outcomes: dict[str, bool]
    markers: dict[str, frozenset[str]]
    xdist_workers: int
    import_graph: ImportGraph


class OutcomeRecordModel(BaseModel):
    """One test's outcome as the profiling hook writes it to the outcomes file."""

    passed: bool
    duration: float
    markers: list[str]


class OutcomesFileModel(BaseModel):
    """Pydantic model for one process's outcomes file.

    Under pytest-xdist each worker writes its own file, so a run produces
    several of these plus an empty one from the controller.

    Attributes:
        worker: The PYTEST_XDIST_WORKER id that wrote this file, None when the
            run was serial.
        worker_count: PYTEST_XDIST_WORKER_COUNT as seen by the writing process,
            None when the run was serial.
        collection_errors: Node ids pytest failed to collect. A run that cannot
            collect a file still measures coverage for everything else, so this
            is the only signal that the profiled suite is missing tests.
    """

    worker: str | None
    worker_count: int | None
    outcomes: dict[str, OutcomeRecordModel]
    collection_errors: list[str]


class ImportEdgeModel(BaseModel):
    """Pydantic model for one import edge."""

    importer: str
    imported: str


class ImportGraphModel(BaseModel):
    """Pydantic model for ImportGraph validation."""

    edges: list[ImportEdgeModel]
    unattributed_modules: list[str]
    resolution_errors: int
    error_samples: list[str]

    def to_import_graph(self) -> ImportGraph:
        """Convert to the internal frozen dataclass."""
        return ImportGraph(
            edges=frozenset(ImportEdge(importer=e.importer, imported=e.imported) for e in self.edges),
            unattributed_modules=frozenset(self.unattributed_modules),
            resolution_errors=self.resolution_errors,
            error_samples=tuple(self.error_samples),
        )


class ProfilingOutcomeModel(BaseModel):
    """Pydantic model for ProfilingOutcome validation."""

    test_id: str
    duration_s: float
    passed: bool
    branches_covered: list[str]
    files_covered: list[str]
    markers: list[str]


class MachineModel(BaseModel):
    """Pydantic model for MachineEnvironment validation."""

    os: str | None = None
    os_version: str | None = None
    platform: str | None = None
    architecture: str | None = None
    cpu_model: str | None = None
    cpu_cores_physical: int | None = None
    cpu_cores_logical: int | None = None
    ram_total_mb: int | None = None
    ram_available_mb: int | None = None
    hostname: str | None = None


class ProfilingMetaModel(BaseModel):
    """Pydantic model for ProfilingMeta validation."""

    timestamp: datetime
    commit: str | None
    python_version: str
    coverage_version: str
    command: str
    machine: MachineModel
    xdist_workers: int


class ProfilingDataFile(BaseModel):
    """Pydantic model for validating profiling data from JSON.

    schema_version is required with no default: a pre-versioning profile
    must fail to validate rather than silently be read as the current
    schema. Check it with load_profiling_data_file before constructing this
    model directly, so a schema mismatch can be reported distinctly from a
    corrupt or unreadable file.
    """

    schema_version: int
    meta: ProfilingMetaModel
    tests: dict[str, ProfilingOutcomeModel]
    total_branches: list[str]
    measured_files: list[str]
    import_graph: ImportGraphModel
    unattributable_branches: list[str] = Field(default_factory=list)

    def to_profiling_data(self) -> ProfilingData:
        """Convert Pydantic model to internal frozen dataclasses."""
        machine_env = MachineEnvironment(**self.meta.machine.model_dump())

        meta = ProfilingMeta(
            timestamp=self.meta.timestamp,
            commit=self.meta.commit,
            python_version=self.meta.python_version,
            coverage_version=self.meta.coverage_version,
            command=self.meta.command,
            machine=machine_env,
            xdist_workers=self.meta.xdist_workers,
        )

        tests = {
            tid: ProfilingOutcome(
                test_id=tr.test_id,
                duration_s=tr.duration_s,
                passed=tr.passed,
                branches_covered=frozenset(tr.branches_covered),
                files_covered=frozenset(tr.files_covered),
                markers=frozenset(tr.markers),
            )
            for tid, tr in self.tests.items()
        }

        return ProfilingData(
            meta=meta,
            tests=tests,
            total_branches=frozenset(self.total_branches),
            measured_files=frozenset(self.measured_files),
            import_graph=self.import_graph.to_import_graph(),
            unattributable_branches=frozenset(self.unattributable_branches),
        )


class ProfileSchemaMismatchError(Exception):
    """A profile's schema_version does not match what this build writes.

    Raised before Pydantic validation runs, so it can be reported to the
    user distinctly from a corrupt or otherwise unreadable file: a schema
    mismatch has one fix (reprofile), while a ValidationError could mean
    anything.
    """

    def __init__(self, found: int | None, expected: int) -> None:
        self.found = found
        self.expected = expected
        super().__init__(f"profile schema version {found!r} does not match expected {expected!r}")


def load_profiling_data_file(raw: Mapping[str, Any]) -> ProfilingDataFile:
    """Validate a raw profiling-data mapping into a ProfilingDataFile.

    Checks schema_version before handing off to Pydantic: an old-schema
    profile can fail ProfilingDataFile's field checks in ways that look
    identical to genuine corruption, and the two need different messages.
    """
    found = raw.get("schema_version")
    if found != PROFILE_SCHEMA_VERSION:
        raise ProfileSchemaMismatchError(found=found, expected=PROFILE_SCHEMA_VERSION)
    return ProfilingDataFile(**raw)
