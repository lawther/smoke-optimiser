import json
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel

from smoke_optimiser.config import ResolvedConfig
from smoke_optimiser.optimiser.models import SmokeResult
from smoke_optimiser.profiler.models import ProfilingMeta
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
    smoke_tests_selected: int
    smoke_branches_covered: int
    smoke_coverage_pct: float
    full_suite_runtime_s: float
    smoke_suite_runtime_s: float


class SmokeSuiteFile(BaseModel):
    """Schema for .smoke_suite.json."""

    version: int = 1
    generated_at: datetime
    generator_version: str = "0.1.0"
    repro_command: str
    machine: dict[str, str | int | None]
    config: dict[str, str | float | list[str] | bool]
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
    # Convert machine environment to dict
    machine_dict = {
        "os": meta.machine.os,
        "os_version": meta.machine.os_version,
        "platform": meta.machine.platform,
        "architecture": meta.machine.architecture,
        "cpu_model": meta.machine.cpu_model,
        "cpu_cores_physical": meta.machine.cpu_cores_physical,
        "cpu_cores_logical": meta.machine.cpu_cores_logical,
        "ram_total_mb": meta.machine.ram_total_mb,
        "ram_available_mb": meta.machine.ram_available_mb,
        "hostname": meta.machine.hostname,
    }

    # Convert config to dict
    config_dict = {
        "time_cap": config.time_cap,
        "target_cov": config.target_cov,
        "include_mandatory": config.include_mandatory,
        "exclude_mandatory": config.exclude_mandatory,
    }

    summary = SummaryModel(
        total_tests_profiled=result.total_tests_profiled,
        tests_passed=result.tests_passed,
        tests_failed=result.tests_failed,
        total_branches=result.total_branches,
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
        machine=machine_dict,
        config=config_dict,
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
