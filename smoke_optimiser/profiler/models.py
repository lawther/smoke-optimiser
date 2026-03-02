from dataclasses import dataclass
from datetime import datetime

from pydantic import BaseModel

from smoke_optimiser.environment import MachineEnvironment


@dataclass(frozen=True)
class ProfilingOutcome:
    """Individual test outcome with duration and coverage."""

    test_id: str
    duration_s: float
    passed: bool
    branches_covered: frozenset[str]
    markers: frozenset[str]


@dataclass(frozen=True)
class ProfilingMeta:
    """Metadata for a profiling run."""

    timestamp: datetime
    commit: str | None
    python_version: str
    coverage_version: str
    command: str
    machine: MachineEnvironment


@dataclass(frozen=True)
class ProfilingData:
    """Complete profiling data for a test suite."""

    meta: ProfilingMeta
    tests: dict[str, ProfilingOutcome]
    total_branches: frozenset[str]


class ProfilingOutcomeModel(BaseModel):
    """Pydantic model for ProfilingOutcome validation."""

    test_id: str
    duration_s: float
    passed: bool
    branches_covered: list[str]
    markers: list[str]


class ProfilingMetaModel(BaseModel):
    """Pydantic model for ProfilingMeta validation."""

    timestamp: datetime
    commit: str | None
    python_version: str
    coverage_version: str
    command: str
    machine: dict[str, str | int | None]


class ProfilingDataFile(BaseModel):
    """Pydantic model for validating profiling data from JSON."""

    meta: ProfilingMetaModel
    tests: dict[str, ProfilingOutcomeModel]
    total_branches: list[str]

    def to_profiling_data(self) -> ProfilingData:
        """Convert Pydantic model to internal frozen dataclasses."""
        machine_env = MachineEnvironment(
            os=self.meta.machine.get("os"),  # type: ignore[arg-type]
            os_version=self.meta.machine.get("os_version"),  # type: ignore[arg-type]
            platform=self.meta.machine.get("platform"),  # type: ignore[arg-type]
            architecture=self.meta.machine.get("architecture"),  # type: ignore[arg-type]
            cpu_model=self.meta.machine.get("cpu_model"),  # type: ignore[arg-type]
            cpu_cores_physical=self.meta.machine.get("cpu_cores_physical"),  # type: ignore[arg-type]
            cpu_cores_logical=self.meta.machine.get("cpu_cores_logical"),  # type: ignore[arg-type]
            ram_total_mb=self.meta.machine.get("ram_total_mb"),  # type: ignore[arg-type]
            ram_available_mb=self.meta.machine.get("ram_available_mb"),  # type: ignore[arg-type]
            hostname=self.meta.machine.get("hostname"),  # type: ignore[arg-type]
        )

        meta = ProfilingMeta(
            timestamp=self.meta.timestamp,
            commit=self.meta.commit,
            python_version=self.meta.python_version,
            coverage_version=self.meta.coverage_version,
            command=self.meta.command,
            machine=machine_env,
        )

        tests = {
            tid: ProfilingOutcome(
                test_id=tr.test_id,
                duration_s=tr.duration_s,
                passed=tr.passed,
                branches_covered=frozenset(tr.branches_covered),
                markers=frozenset(tr.markers),
            )
            for tid, tr in self.tests.items()
        }

        return ProfilingData(
            meta=meta,
            tests=tests,
            total_branches=frozenset(self.total_branches),
        )
