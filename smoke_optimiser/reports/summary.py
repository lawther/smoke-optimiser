from smoke_optimiser.config import ResolvedConfig
from smoke_optimiser.optimiser.models import SmokeResult
from smoke_optimiser.profiler.models import ProfilingMeta
from smoke_optimiser.reports.repro import build_repro_command


def format_summary(result: SmokeResult, config: ResolvedConfig, meta: ProfilingMeta) -> str:
    """Format a human-readable summary report of the optimisation results."""
    repro = build_repro_command(config)

    pct_runtime = (
        (result.smoke_suite_runtime_s / result.full_suite_runtime_s * 100.0) if result.full_suite_runtime_s > 0 else 0.0
    )

    machine_line1 = f"  Machine:      {meta.machine.os} {meta.machine.architecture} — {meta.machine.cpu_model}"
    machine_line2 = (
        f"                {meta.machine.cpu_cores_physical} cores / "
        f"{meta.machine.cpu_cores_logical} threads, {meta.machine.ram_total_mb} MB RAM"
    )

    profiled_line = (
        f"  Profiled:     {result.tests_passed} passed, {result.tests_failed} failed "
        f"({result.total_tests_profiled} total)"
    )

    coverage_line = (
        f"  Coverage:     {result.smoke_branches_covered:,} / "
        f"{result.total_branches:,} branches ({result.smoke_coverage_pct:.1f}%)"
    )

    runtime_line = f"  Runtime:      {result.smoke_suite_runtime_s:.1f}s ({pct_runtime:.1f}% of full suite)"

    lines = [
        "═══════════════════════════════════════════════",
        "  smoke-optimiser results",
        "═══════════════════════════════════════════════",
        "",
        machine_line1,
        machine_line2,
        f"                {meta.machine.hostname}",
        "",
        profiled_line,
        f"  Full suite:   {result.full_suite_runtime_s:.1f}s runtime, {result.total_branches:,} branches",
        "",
        f"  Smoke suite:  {len(result.selected_tests)} tests selected",
        coverage_line,
        runtime_line,
        "",
        "  Repro:        " + repro,
        "",
        f"  Saved to:     {config.output_json}",
        "",
        f"  Coverage-equivalent groups: {len(result.coverage_equivalents)} groups",
    ]

    if result.tests_failed > 0:
        lines.append(f"  ⚠ {result.tests_failed} failing tests were excluded.")

    lines.append("═══════════════════════════════════════════════")

    return "\n".join(lines)
