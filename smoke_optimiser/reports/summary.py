import typer

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

    passed_str = typer.style(f"{result.tests_passed} passed", fg=typer.colors.GREEN)
    failed_str = (
        typer.style(f"{result.tests_failed} failed", fg=typer.colors.RED)
        if result.tests_failed > 0
        else f"{result.tests_failed} failed"
    )
    profiled_line = (
        f"  Profiled:     {passed_str}, {failed_str} "
        f"({result.total_tests_profiled} total)"
    )

    full_suite_cov_pct = (
        (result.full_suite_branches_covered / result.total_branches * 100.0) if result.total_branches > 0 else 0.0
    )
    full_suite_cov_line = (
        f"  Coverage:     {result.full_suite_branches_covered:,} / "
        f"{result.total_branches:,} branches ({full_suite_cov_pct:.1f}%)"
    )

    smoke_cov_str = (
        f"{result.smoke_branches_covered:,} / {result.total_branches:,} branches ({result.smoke_coverage_pct:.1f}%)"
    )
    smoke_coverage_line = f"  Coverage:     {typer.style(smoke_cov_str, fg=typer.colors.CYAN)}"

    runtime_str = f"{result.smoke_suite_runtime_s:.1f}s ({pct_runtime:.1f}% of full suite)"
    runtime_line = f"  Runtime:      {typer.style(runtime_str, fg=typer.colors.GREEN)}"

    lines = [
        typer.style("═══════════════════════════════════════════════", fg=typer.colors.BLUE, bold=True),
        typer.style("  smoke-optimiser results", bold=True),
        typer.style("═══════════════════════════════════════════════", fg=typer.colors.BLUE, bold=True),
        "",
        machine_line1,
        machine_line2,
        f"                {meta.machine.hostname}",
        "",
        profiled_line,
        f"  Full suite:   {result.full_suite_runtime_s:.1f}s runtime, {result.total_branches:,} branches",
        full_suite_cov_line,
        "",
        f"  Smoke suite:  {typer.style(f'{len(result.selected_tests)} tests selected', bold=True)}",
        smoke_coverage_line,
        runtime_line,
        "",
        f"  Repro:        {typer.style(repro, fg=typer.colors.MAGENTA)}",
        "",
        f"  Saved to:     {typer.style(str(config.output_json), bold=True)}",
        "",
        f"  Coverage-equivalent groups: {len(result.coverage_equivalents)} groups",
    ]

    if result.tests_failed > 0:
        warning_str = f"⚠ {result.tests_failed} failing tests were excluded."
        lines.append(f"  {typer.style(warning_str, fg=typer.colors.RED, bold=True)}")

    lines.append(typer.style("═══════════════════════════════════════════════", fg=typer.colors.BLUE, bold=True))

    return "\n".join(lines)
