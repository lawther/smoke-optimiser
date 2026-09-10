from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, NamedTuple

from pydantic import BaseModel, Field

from smoke_optimiser.environment import MachineEnvironment
from smoke_optimiser.profiler.scope import ProfileScope

PROFILE_SCHEMA_VERSION = 5
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
        files_read: Non-Python project files this test opened for reading, in
            any of setup, call or teardown. Coverage and the import graph both
            answer only for Python, so this is the sole relation a data file
            has to a test.
        directories_listed: Project directories this test listed or globbed. A
            test that globs a directory never names the file it opens, so this
            is what answers for a file added after the profile was taken.
    """

    test_id: str
    duration_s: float
    passed: bool
    branches_covered: frozenset[str]
    files_covered: frozenset[str]
    markers: frozenset[str]
    files_read: frozenset[str] = frozenset()
    directories_listed: frozenset[str] = frozenset()


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
class ReadMap:
    """What one process's read tracer saw, before it is folded into the profile.

    The per-test halves land on each :class:`ProfilingOutcome`; what stays
    here is what belongs to no single test.

    Attributes:
        reads_by_test: Node id -> the non-Python files that test read.
        listings_by_test: Node id -> the directories that test listed.
        unattributed_reads: Files read outside any test -- at import time or
            during collection. A change to one means the map CANNOT ANSWER,
            which must fall back to running everything: reading it as "no test
            reads this" would select nothing at all.
        recording_errors: How many times recording a read failed and was
            swallowed. Each swallowed failure is a MISSING read, and a missing
            read silently under-selects, so a non-zero count means the map is
            incomplete by an unknown amount.
        error_samples: The first few of those failures, for diagnosis.
    """

    reads_by_test: Mapping[str, frozenset[str]]
    listings_by_test: Mapping[str, frozenset[str]]
    unattributed_reads: frozenset[str]
    recording_errors: int
    error_samples: tuple[str, ...]


@dataclass(frozen=True)
class ReadObservations:
    """The part of the read map that belongs to no single test.

    Separate from :class:`ReadMap` because the per-test halves of that map are
    stored on the outcomes themselves, leaving the profile to carry only what
    qualifies the answers those relations give -- the same split
    :class:`ImportGraph` makes between its edges and its own trustworthiness.
    """

    unattributed_reads: frozenset[str] = frozenset()
    recording_errors: int = 0
    error_samples: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProfilingMeta:
    """Metadata for a profiling run.

    Attributes:
        xdist_workers: Number of pytest-xdist workers the suite ran under, 1 for
            a serial run. Above 1, every duration was measured while that many
            tests competed for the machine, so the durations are not comparable
            with each other or with a serial profile. The coverage map is
            unaffected.
        iterations: How many complete passes over the suite each duration is the
            mean of -- the number that FINISHED, not the number configured, so a
            three-iteration run whose last pass was interrupted records two. The
            maps do not vary with it (they merge where the durations average), so
            this says nothing about the coverage, import or read maps and
            everything about how much a single slow sample could be skewing the
            ranking. A downwind fallback records 1 whatever the project
            configured.
    """

    timestamp: datetime
    commit: str | None
    python_version: str
    coverage_version: str
    command: str
    machine: MachineEnvironment
    xdist_workers: int
    iterations: int


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
        present_files: Every non-ignored file in the working tree when the run
            started, Python included. This is the profile's denominator:
            without it, "nothing the run watched touched this file" and "this
            file was not there to be touched" are the same observation, and
            only the first of them can warrant selecting nothing. The read map
            asks it of a data file nothing opened; the Python maps ask it of a
            module that appears in none of them.
        reads: What the read tracer saw that belongs to no single test, and how
            far it can be trusted.
        scope: What this profile was captured over -- the coverage targets and
            test paths the profiling run itself resolved. A file in scope is one
            a regenerated profile would know about, which is what makes
            comparing the tree against the maps meaningful rather than a
            comparison every profile fails on its own non-Python files.
    """

    meta: ProfilingMeta
    tests: dict[str, ProfilingOutcome]
    total_branches: frozenset[str]
    measured_files: frozenset[str]
    import_graph: ImportGraph
    scope: ProfileScope
    unattributable_branches: frozenset[str] = frozenset()
    reads: ReadObservations = ReadObservations()
    present_files: frozenset[str] = frozenset()


@dataclass(frozen=True)
class SuiteRunResults:
    """Per-test facts gathered by the pytest hook, keyed by node id.

    Attributes:
        scope: The coverage targets and test paths the profiled pytest process
            resolved. Read inside that process because that is the only place a
            project's own addopts and testpaths have been applied.
        iterations: How many passes over the suite these durations are the mean
            of. Counted from the passes that finished, so an interrupted run
            reports what it kept rather than what it set out to do.
    """

    durations: dict[str, float]
    outcomes: dict[str, bool]
    markers: dict[str, frozenset[str]]
    xdist_workers: int
    iterations: int
    import_graph: ImportGraph
    scope: ProfileScope
    read_map: ReadMap
    present_files: frozenset[str]


class ProfileScopeModel(BaseModel):
    """Pydantic model for ProfileScope validation."""

    coverage_roots: list[str]
    test_roots: list[str]
    test_file_patterns: list[str]
    include_namespace_packages: bool

    def to_profile_scope(self) -> ProfileScope:
        """Convert to the internal frozen dataclass."""
        return ProfileScope(
            coverage_roots=frozenset(self.coverage_roots),
            test_roots=frozenset(self.test_roots),
            test_file_patterns=tuple(self.test_file_patterns),
            include_namespace_packages=self.include_namespace_packages,
        )

    @classmethod
    def from_profile_scope(cls, scope: ProfileScope) -> "ProfileScopeModel":
        """Build the model that writes ``scope`` to a file, ordered for a stable diff."""
        return cls(
            coverage_roots=sorted(scope.coverage_roots),
            test_roots=sorted(scope.test_roots),
            test_file_patterns=list(scope.test_file_patterns),
            include_namespace_packages=scope.include_namespace_packages,
        )


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
        scope: The coverage targets and test paths this process resolved. Every
            process writes them; a serial run and an xdist worker resolve the
            same command line, so the runner unions them.
    """

    worker: str | None
    worker_count: int | None
    outcomes: dict[str, OutcomeRecordModel]
    collection_errors: list[str]
    scope: ProfileScopeModel


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


class ReadMapModel(BaseModel):
    """Pydantic model for one process's read map, as the profiling hook writes it."""

    reads_by_test: dict[str, list[str]]
    listings_by_test: dict[str, list[str]]
    unattributed_reads: list[str]
    recording_errors: int
    error_samples: list[str]

    def to_read_map(self) -> ReadMap:
        """Convert to the internal frozen dataclass."""
        return ReadMap(
            reads_by_test={test: frozenset(paths) for test, paths in self.reads_by_test.items()},
            listings_by_test={test: frozenset(paths) for test, paths in self.listings_by_test.items()},
            unattributed_reads=frozenset(self.unattributed_reads),
            recording_errors=self.recording_errors,
            error_samples=tuple(self.error_samples),
        )

    @classmethod
    def from_read_map(cls, read_map: ReadMap) -> "ReadMapModel":
        """Build the model that writes ``read_map`` to a file, ordered for a stable diff."""
        return cls(
            reads_by_test={test: sorted(paths) for test, paths in sorted(read_map.reads_by_test.items())},
            listings_by_test={test: sorted(paths) for test, paths in sorted(read_map.listings_by_test.items())},
            unattributed_reads=sorted(read_map.unattributed_reads),
            recording_errors=read_map.recording_errors,
            error_samples=list(read_map.error_samples),
        )


class ReadObservationsModel(BaseModel):
    """Pydantic model for ReadObservations validation."""

    unattributed_reads: list[str] = Field(default_factory=list)
    recording_errors: int = 0
    error_samples: list[str] = Field(default_factory=list)

    def to_read_observations(self) -> ReadObservations:
        """Convert to the internal frozen dataclass."""
        return ReadObservations(
            unattributed_reads=frozenset(self.unattributed_reads),
            recording_errors=self.recording_errors,
            error_samples=tuple(self.error_samples),
        )

    @classmethod
    def from_read_observations(cls, reads: ReadObservations) -> "ReadObservationsModel":
        """Build the model that writes ``reads`` to the profile, ordered for a stable diff."""
        return cls(
            unattributed_reads=sorted(reads.unattributed_reads),
            recording_errors=reads.recording_errors,
            error_samples=list(reads.error_samples),
        )


class ProfilingOutcomeModel(BaseModel):
    """Pydantic model for ProfilingOutcome validation."""

    test_id: str
    duration_s: float
    passed: bool
    branches_covered: list[str]
    files_covered: list[str]
    markers: list[str]
    files_read: list[str] = Field(default_factory=list)
    directories_listed: list[str] = Field(default_factory=list)


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
    iterations: int


class ProfilingDataFile(BaseModel):
    """Pydantic model for validating profiling data from JSON.

    schema_version is required with no default: a pre-versioning profile
    must fail to validate rather than silently be read as the current
    schema. Check it with load_profiling_data_file before constructing this
    model directly, so a schema mismatch can be reported distinctly from a
    corrupt or unreadable file, and so a scope naming nothing is caught
    rather than read as a profile that knows about nothing.
    """

    schema_version: int
    meta: ProfilingMetaModel
    tests: dict[str, ProfilingOutcomeModel]
    total_branches: list[str]
    measured_files: list[str]
    import_graph: ImportGraphModel
    scope: ProfileScopeModel
    unattributable_branches: list[str] = Field(default_factory=list)
    reads: ReadObservationsModel = Field(default_factory=ReadObservationsModel)
    present_files: list[str] = Field(default_factory=list)

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
            iterations=self.meta.iterations,
        )

        tests = {
            tid: ProfilingOutcome(
                test_id=tr.test_id,
                duration_s=tr.duration_s,
                passed=tr.passed,
                branches_covered=frozenset(tr.branches_covered),
                files_covered=frozenset(tr.files_covered),
                markers=frozenset(tr.markers),
                files_read=frozenset(tr.files_read),
                directories_listed=frozenset(tr.directories_listed),
            )
            for tid, tr in self.tests.items()
        }

        return ProfilingData(
            meta=meta,
            tests=tests,
            total_branches=frozenset(self.total_branches),
            measured_files=frozenset(self.measured_files),
            import_graph=self.import_graph.to_import_graph(),
            scope=self.scope.to_profile_scope(),
            unattributable_branches=frozenset(self.unattributable_branches),
            reads=self.reads.to_read_observations(),
            present_files=frozenset(self.present_files),
        )


class ProfileSchemaMismatchError(Exception):
    """A profile's schema_version does not match what this build writes.

    Raised before Pydantic validation runs, so it can be reported to the
    user distinctly from a corrupt or otherwise unreadable file: a schema
    mismatch has one fix (reprofile), while a ValidationError could mean
    anything.

    Attributes:
        command: The command that produced the stale profile, read straight
            from the raw JSON rather than through Pydantic -- schema
            validation is exactly what this file has already failed, so the
            field is recovered on a best-effort basis and is None whenever
            the mismatched file predates recording it, or was corrupt there
            too. When present, it lets the error message hand the user a
            ready-to-run fix instead of just naming the problem.
    """

    def __init__(self, found: int | None, expected: int, command: str | None = None) -> None:
        self.found = found
        self.expected = expected
        self.command = command
        super().__init__(f"profile schema version {found!r} does not match expected {expected!r}")


class ProfileScopeMissingError(Exception):
    """A profile of the current schema records no scope roots.

    The scope is what makes a comparison between the tree and the maps
    meaningful, so a profile without one cannot say whether it has gone
    stale. That is reported rather than treated as an empty scope: an empty
    scope puts no file in scope, so nothing ever looks diverged and the
    profile answers confidently forever.
    """

    def __init__(self, schema_version: int) -> None:
        self.schema_version = schema_version
        super().__init__(f"schema version {schema_version} profile records no scope roots")


def load_profiling_data_file(raw: Mapping[str, Any]) -> ProfilingDataFile:
    """Validate a raw profiling-data mapping into a ProfilingDataFile.

    Checks schema_version before handing off to Pydantic: an old-schema
    profile can fail ProfilingDataFile's field checks in ways that look
    identical to genuine corruption, and the two need different messages.
    A scope that names nothing gets a third message again, because the fix
    is not the same as either.
    """
    found = raw.get("schema_version")
    if found != PROFILE_SCHEMA_VERSION:
        meta = raw.get("meta")
        command = meta.get("command") if isinstance(meta, Mapping) else None
        raise ProfileSchemaMismatchError(
            found=found,
            expected=PROFILE_SCHEMA_VERSION,
            command=command if isinstance(command, str) else None,
        )
    validated = ProfilingDataFile(**raw)
    if validated.scope.to_profile_scope().is_empty:
        raise ProfileScopeMissingError(schema_version=PROFILE_SCHEMA_VERSION)
    return validated
