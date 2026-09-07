import json
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError

from smoke_optimiser.config import FileConfig, OperationMode, ResolvedConfig, load_file_config, resolve_config
from smoke_optimiser.optimiser.filters import apply_filters
from smoke_optimiser.optimiser.greedy import optimise
from smoke_optimiser.profiler.models import (
    PROFILE_SCHEMA_VERSION,
    ImportEdgeModel,
    ImportGraphModel,
    MachineModel,
    ProfileSchemaMismatchError,
    ProfilingData,
    ProfilingDataFile,
    ProfilingMetaModel,
    ProfilingOutcomeModel,
    load_profiling_data_file,
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


def _save_profiling_data(profiling_data: ProfilingData, intermediate_file: Path) -> None:
    """Save profiling data to an intermediate JSON file."""
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
        xdist_workers=profiling_data.meta.xdist_workers,
    )
    test_models = {
        tid: ProfilingOutcomeModel(
            test_id=po.test_id,
            duration_s=po.duration_s,
            passed=po.passed,
            branches_covered=list(po.branches_covered),
            files_covered=list(po.files_covered),
            markers=list(po.markers),
        )
        for tid, po in profiling_data.tests.items()
    }
    graph = profiling_data.import_graph
    graph_model = ImportGraphModel(
        edges=[ImportEdgeModel(importer=edge.importer, imported=edge.imported) for edge in sorted(graph.edges)],
        unattributed_modules=sorted(graph.unattributed_modules),
        resolution_errors=graph.resolution_errors,
        error_samples=list(graph.error_samples),
    )
    file_data = ProfilingDataFile(
        schema_version=PROFILE_SCHEMA_VERSION,
        meta=meta_model,
        tests=test_models,
        total_branches=list(profiling_data.total_branches),
        measured_files=list(profiling_data.measured_files),
        import_graph=graph_model,
        unattributable_branches=list(profiling_data.unattributable_branches),
    )
    intermediate_file.unlink(missing_ok=True)
    with intermediate_file.open("w") as f:
        json.dump(file_data.model_dump(mode="json"), f)
    typer.secho(f"💾 Profiling data saved to {intermediate_file}", fg=typer.colors.GREEN)


def _load_profiling_data(intermediate_file: Path) -> ProfilingData:
    """Load profiling data from an intermediate JSON file."""
    # Try to load from intermediate file if it exists
    if not intermediate_file.exists():
        typer.secho(
            "❌ Error: No profiling data found. Run without --optimise-only first.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)

    try:
        with intermediate_file.open("rb") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        typer.secho(
            f"❌ Error: Failed to parse profiling data ({intermediate_file}): {e}",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1) from None

    try:
        return load_profiling_data_file(raw).to_profiling_data()
    except ProfileSchemaMismatchError as e:
        typer.secho(
            f"❌ Error: Profiling data ({intermediate_file}) has schema version {e.found!r}, but this build "
            f"expects schema version {e.expected}. Re-run the profiling phase to regenerate it.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1) from None
    except ValidationError as e:
        typer.secho(
            f"❌ Error: Failed to parse profiling data ({intermediate_file}): {e}",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1) from None


def _validate_option_combinations(
    *,
    profile_only: bool,
    optimise_only: bool,
    src: str | None,
    pytest_args: str | None,
) -> None:
    """Reject option combinations that cannot be honoured."""
    if profile_only and optimise_only:
        typer.secho(
            "❌ Error: --profile-only and --optimise-only are mutually exclusive.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)

    if src and pytest_args and "--cov" in pytest_args:
        typer.secho(
            "❌ Error: Conflict detected. Cannot use --src and --cov in --pytest-args simultaneously.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)


def _warn_if_source_was_guessed(
    config: ResolvedConfig,
    file_config: FileConfig | None,
    src: str | None,
    pytest_args: str | None,
) -> None:
    """Tell the user when the coverage source came from heuristic discovery rather than from them."""
    if config.mode == OperationMode.OPTIMISE_ONLY or src is not None:
        return
    if pytest_args and "--cov" in pytest_args:
        return
    if file_config and file_config.cov_source:
        return

    typer.secho(
        f"⚠️ Warning: --src was not specified. Falling back to heuristic discovery: --src={config.cov_source}",
        fg=typer.colors.YELLOW,
        err=True,
    )


def _reject_parallel_durations(config: ResolvedConfig, profiling_data: ProfilingData) -> None:
    """Refuse to rank a profile whose durations were measured under contention.

    The optimiser picks tests by coverage per second, so a profile recorded with
    pytest-xdist ranks tests by how much they had to compete for the machine
    rather than by how long they take. The resulting suite looks perfectly
    ordinary and is quietly wrong, which is why this is an error and not a
    warning. The coverage map itself is unaffected, so recording such a profile
    is fine -- only ranking it is not.
    """
    workers = profiling_data.meta.xdist_workers
    if workers <= 1 or config.allow_parallel_durations:
        return

    typer.secho(
        f"❌ Error: this profile was recorded with {workers} pytest-xdist workers, so every duration "
        "was measured while other tests competed for the machine. Ranking tests by coverage per "
        "second on those timings gives a suite that looks right and is not.\n"
        "  Hint: re-profile serially, or pass --allow-parallel-durations to rank them anyway.",
        fg=typer.colors.RED,
        err=True,
    )
    raise typer.Exit(code=1)


def _optimise_and_report(config: ResolvedConfig, profiling_data: ProfilingData) -> None:
    """Run the optimisation phase and write out its results."""
    _reject_parallel_durations(config, profiling_data)
    typer.secho("⚡ Optimising smoke suite...", fg=typer.colors.CYAN, bold=True)
    filtered = apply_filters(profiling_data.tests, config.include_mandatory, config.exclude_mandatory)

    for pattern in filtered.unmatched_includes:
        typer.secho(
            f"⚠️ Warning: Include pattern '{pattern}' matched no tests.",
            fg=typer.colors.YELLOW,
            err=True,
        )
    for pattern in filtered.unmatched_excludes:
        typer.secho(
            f"⚠️ Warning: Exclude pattern '{pattern}' matched no tests.",
            fg=typer.colors.YELLOW,
            err=True,
        )

    result = optimise(
        filtered,
        profiling_data.total_branches,
        config.time_cap,
        config.target_cov,
        profiling_data.unattributable_branches,
    )

    write_smoke_suite(result, config, profiling_data.meta, config.output_json)
    typer.echo(format_summary(result, config, profiling_data.meta))


@app.command()
def main(  # noqa: PLR0913 # special case for this function since Typer works this way
    *,
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
    allow_parallel_durations: Annotated[
        bool,
        typer.Option(
            "--allow-parallel-durations",
            help="Build a smoke suite from a profile recorded with pytest-xdist, whose durations "
            "were measured under contention.",
        ),
    ] = False,
) -> None:
    """smoke-optimiser: Identify a minimal, high-value smoke test suite."""
    _validate_option_combinations(
        profile_only=profile_only,
        optimise_only=optimise_only,
        src=src,
        pytest_args=pytest_args,
    )

    # Normalise comma-separated includes/excludes
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
        "allow_parallel_durations": allow_parallel_durations or None,
    }

    project_root = Path.cwd()
    try:
        file_config = load_file_config(project_root)
        config = resolve_config(file_config, cli_overrides, project_root)
    except ValidationError as err:
        typer.secho("❌ Error: Invalid configuration in pyproject.toml", fg=typer.colors.RED, err=True)
        for error in err.errors():
            loc = ".".join(str(loc) for loc in error["loc"])
            typer.secho(f"  - {loc}: {error['msg']}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from None

    _warn_if_source_was_guessed(config, file_config, src, pytest_args)

    profiling_data = None
    intermediate_file = project_root / ".smoke_profiling_data.json"

    # Phase 1: Profiling
    if config.mode != OperationMode.OPTIMISE_ONLY:
        typer.secho("🔍 Running profiling...", fg=typer.colors.CYAN, bold=True)
        profiling_data = run_profiling(config, project_root)

        if config.mode == OperationMode.PROFILE_ONLY:
            _save_profiling_data(profiling_data, intermediate_file)

    # Phase 2: Optimisation
    if config.mode != OperationMode.PROFILE_ONLY:
        if profiling_data is None:
            profiling_data = _load_profiling_data(intermediate_file)

        _optimise_and_report(config, profiling_data)


if __name__ == "__main__":
    app()
