import json
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel

from smoke_optimiser.config import ResolvedConfig
from smoke_optimiser.optimiser.models import SmokeResult
from smoke_optimiser.profiler.models import MachineModel, ProfilingMeta
from smoke_optimiser.reports.repro import build_repro_command


class SelectedTestModel(BaseModel):
    test_id: str
    duration_s: float
    branches_covered: int
    marginal_branches: int
    efficiency: float


class CoverageEquivalentGroupModel(BaseModel):
    group_id: int
    branch_set_hash: str
    tests: list[str]


class SummaryModel(BaseModel):
    total_tests_profiled: int
    tests_passed: int
    tests_failed: int
    total_branches: int
    full_suite_branches_covered: int
    smoke_tests_selected: int
    smoke_branches_covered: int
    smoke_coverage_pct: float
    full_suite_runtime_s: float
    smoke_suite_runtime_s: float


class SmokeConfigModel(BaseModel):
    """Configuration saved in the smoke suite file."""

    time_cap: float = 0.0
    target_cov: float = 0.0
    include_mandatory: list[str] = []
    exclude_mandatory: list[str] = []


class SmokeSuiteFile(BaseModel):
    """Schema for .smoke_suite.json."""

    version: int = 1
    generated_at: datetime
    generator_version: str = "0.1.0"
    repro_command: str
    machine: MachineModel
    config: SmokeConfigModel
    summary: SummaryModel
    smoke_tests: list[SelectedTestModel]
    coverage_equivalents: list[CoverageEquivalentGroupModel]


def write_smoke_suite(
    result: SmokeResult,
    config: ResolvedConfig,
    meta: ProfilingMeta,
    output_path: Path,
) -> None:
    """Write the smoke suite definition to a JSON file."""
    # Convert machine environment to model
    machine = meta.machine
    machine_model = MachineModel(
        os=machine.os,
        os_version=machine.os_version,
        platform=machine.platform,
        architecture=machine.architecture,
        cpu_model=machine.cpu_model,
        cpu_cores_physical=machine.cpu_cores_physical,
        cpu_cores_logical=machine.cpu_cores_logical,
        ram_total_mb=machine.ram_total_mb,
        ram_available_mb=machine.ram_available_mb,
        hostname=machine.hostname,
    )

    # Convert config to model
    config_model = SmokeConfigModel(
        time_cap=config.time_cap,
        target_cov=config.target_cov,
        include_mandatory=config.include_mandatory,
        exclude_mandatory=config.exclude_mandatory,
    )

    summary = SummaryModel(
        total_tests_profiled=result.total_tests_profiled,
        tests_passed=result.tests_passed,
        tests_failed=result.tests_failed,
        total_branches=result.total_branches,
        full_suite_branches_covered=result.full_suite_branches_covered,
        smoke_tests_selected=len(result.selected_tests),
        smoke_branches_covered=result.smoke_branches_covered,
        smoke_coverage_pct=result.smoke_coverage_pct,
        full_suite_runtime_s=result.full_suite_runtime_s,
        smoke_suite_runtime_s=result.smoke_suite_runtime_s,
    )

    smoke_tests = [
        SelectedTestModel(
            test_id=t.test_id,
            duration_s=t.duration_s,
            branches_covered=t.branches_covered,
            marginal_branches=t.marginal_branches,
            efficiency=t.efficiency,
        )
        for t in result.selected_tests
    ]

    equivalents = [
        CoverageEquivalentGroupModel(
            group_id=eg.group_id,
            branch_set_hash=eg.branch_set_hash,
            tests=list(eg.tests),
        )
        for eg in result.coverage_equivalents
    ]

    suite = SmokeSuiteFile(
        generated_at=meta.timestamp,
        repro_command=build_repro_command(config),
        machine=machine_model,
        config=config_model,
        summary=summary,
        smoke_tests=smoke_tests,
        coverage_equivalents=equivalents,
    )

    with open(output_path, "w") as f:
        json.dump(suite.model_dump(mode="json"), f, indent=2)


def read_smoke_suite(path: Path) -> SmokeSuiteFile:
    """Read and validate a smoke suite file."""
    with open(path, "rb") as f:
        data = json.load(f)
    return SmokeSuiteFile(**data)
