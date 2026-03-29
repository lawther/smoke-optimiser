import json
from pathlib import Path
from typing import Annotated

import typer

from smoke_optimiser.config import OperationMode, load_file_config, resolve_config
from smoke_optimiser.optimiser.filters import apply_filters
from smoke_optimiser.optimiser.greedy import optimise
from smoke_optimiser.profiler.models import (
    MachineModel,
    ProfilingDataFile,
    ProfilingMetaModel,
    ProfilingOutcomeModel,
)
from smoke_optimiser.profiler.runner import run_profiling
from smoke_optimiser.reports.smoke_suite import write_smoke_suite
from smoke_optimiser.reports.summary import format_summary

app = typer.Typer(pretty_exceptions_show_locals=False)


def _split_comma_list(items: list[str] | None) -> list[str]:
    """Split comma-separated strings in a list into individual items."""
    if items is None:
        return []
    result = []
    for item in items:
        if "," in item:
            result.extend([x.strip() for x in item.split(",") if x.strip()])
        elif item.strip():
            result.append(item.strip())
    return result


@app.command()
def main(
    profile_only: Annotated[
        bool,
        typer.Option("--profile-only", help="Run only the profiling phase."),
    ] = False,
    optimise_only: Annotated[
        bool,
        typer.Option("--optimise-only", help="Run only the optimisation phase."),
    ] = False,
    time_cap: Annotated[
        float | None,
        typer.Option("--time-cap", help="Maximum wall-clock runtime of the smoke suite."),
    ] = None,
    target_cov: Annotated[
        float | None,
        typer.Option(
            "--target-cov",
            help="Target percentage of the full suite's branch coverage to achieve.",
        ),
    ] = None,
    include: Annotated[
        list[str] | None,
        typer.Option("--include", help="Tests or markers that MUST be in the smoke suite."),
    ] = None,
    exclude: Annotated[
        list[str] | None,
        typer.Option("--exclude", help="Tests or markers that MUST NOT be in the smoke suite."),
    ] = None,
    pytest_args: Annotated[
        str | None,
        typer.Option("--pytest-args", help="Extra arguments forwarded to pytest."),
    ] = None,
    output_json: Annotated[
        Path | None,
        typer.Option("--output-json", help="Path for the smoke suite definition file."),
    ] = None,
    allow_ordered: Annotated[
        bool,
        typer.Option(
            "--allow-ordered",
            help="Suppress error when pytest-randomly is not installed.",
        ),
    ] = False,
    src: Annotated[
        str | None,
        typer.Option("--src", help="Source directory/package for coverage instrumentation."),
    ] = None,
    iterations: Annotated[
        int | None,
        typer.Option("--iterations", help="Number of times to run the suite to average timing."),
    ] = None,
) -> None:
    """smoke-optimiser: Identify a minimal, high-value smoke test suite."""
    if profile_only and optimise_only:
        typer.secho(
            "❌ Error: --profile-only and --optimise-only are mutually exclusive.", fg=typer.colors.RED, err=True
        )
        raise typer.Exit(code=1)

    # Conflict check: --src and --cov in --pytest-args
    if src and pytest_args and "--cov" in pytest_args:
        typer.secho(
            "❌ Error: Conflict detected. Cannot use --src and --cov in --pytest-args simultaneously.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)

    # Normalize comma-separated includes/excludes
    final_includes = _split_comma_list(include)
    final_excludes = _split_comma_list(exclude)

    # Collect CLI overrides
    cli_overrides = {
        "profile_only": profile_only,
        "optimise_only": optimise_only,
        "time_cap": time_cap,
        "target_cov": target_cov,
        "include_mandatory": final_includes if include is not None else None,
        "exclude_mandatory": final_excludes if exclude is not None else None,
        "pytest_args": pytest_args,
        "output_json": output_json,
        "allow_ordered": allow_ordered,
        "cov_source": src,
        "iterations": iterations,
    }

    project_root = Path.cwd()
    file_config = load_file_config(project_root)
    config = resolve_config(file_config, cli_overrides, project_root)

    # Inform user about heuristic fallback
    # If it wasn't in CLI and isn't in pytest_args, and we are profiling
    has_cov_in_args = pytest_args and "--cov" in pytest_args
    if config.mode != OperationMode.OPTIMISE_ONLY and src is None and not has_cov_in_args:
        # Check if it was in pyproject.toml
        from_file = file_config and file_config.cov_source
        if not from_file:
            typer.secho(
                f"⚠ Warning: --src was not specified. Falling back to heuristic discovery: --src={config.cov_source}",
                fg=typer.colors.YELLOW,
                err=True,
            )

    profiling_data = None
    intermediate_file = project_root / ".smoke_profiling_data.json"

    # Phase 1: Profiling
    if config.mode != OperationMode.OPTIMISE_ONLY:
        typer.secho("🔍 Running profiling...", fg=typer.colors.CYAN, bold=True)
        profiling_data = run_profiling(config, project_root)

        if config.mode == OperationMode.PROFILE_ONLY:
            # Save intermediate data
            machine = profiling_data.meta.machine
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
            meta_model = ProfilingMetaModel(
                timestamp=profiling_data.meta.timestamp,
                commit=profiling_data.meta.commit,
                python_version=profiling_data.meta.python_version,
                coverage_version=profiling_data.meta.coverage_version,
                command=profiling_data.meta.command,
                machine=machine_model,
            )
            test_models = {
                tid: ProfilingOutcomeModel(
                    test_id=po.test_id,
                    duration_s=po.duration_s,
                    passed=po.passed,
                    branches_covered=list(po.branches_covered),
                    markers=list(po.markers),
                )
                for tid, po in profiling_data.tests.items()
            }
            file_data = ProfilingDataFile(
                meta=meta_model,
                tests=test_models,
                total_branches=list(profiling_data.total_branches),
            )
            intermediate_file.unlink(missing_ok=True)
            with open(intermediate_file, "w") as f:
                json.dump(file_data.model_dump(mode="json"), f)
            typer.secho(f"💾 Profiling data saved to {intermediate_file}", fg=typer.colors.GREEN)

    # Phase 2: Optimisation
    if config.mode != OperationMode.PROFILE_ONLY:
        if profiling_data is None:
            # Try to load from intermediate file if it exists
            if not intermediate_file.exists():
                typer.secho(
                    "❌ Error: No profiling data found. Run without --optimise-only first.",
                    fg=typer.colors.RED,
                    err=True,
                )
                raise typer.Exit(code=1)

            with open(intermediate_file, "rb") as f:
                raw = json.load(f)
                profiling_data = ProfilingDataFile(**raw).to_profiling_data()

        typer.secho("⚡ Optimising smoke suite...", fg=typer.colors.CYAN, bold=True)
        filtered = apply_filters(profiling_data.tests, config.include_mandatory, config.exclude_mandatory)

        # Warn about unmatched includes/excludes
        for pattern in filtered.unmatched_includes:
            typer.secho(
                f"⚠ Warning: include pattern '{pattern}' matched no tests.",
                fg=typer.colors.YELLOW,
                err=True,
            )
        for pattern in filtered.unmatched_excludes:
            typer.secho(
                f"⚠ Warning: exclude pattern '{pattern}' matched no tests.",
                fg=typer.colors.YELLOW,
                err=True,
            )

        result = optimise(filtered, profiling_data.total_branches, config.time_cap, config.target_cov)

        # Output results
        write_smoke_suite(result, config, profiling_data.meta, config.output_json)
        typer.echo(format_summary(result, config, profiling_data.meta))


if __name__ == "__main__":
    app()
