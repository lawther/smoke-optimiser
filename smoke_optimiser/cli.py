"""The two selection modes, as two commands.

``smoke`` is the coverage-per-second path: profile the suite, then pick the
highest-value subset that fits a time cap. ``downwind`` is the categorical
one: given what changed, run every test the profile says those changes can
reach. They are separate commands because they answer different questions
from the same profile, and almost none of the options of one mean anything
to the other.
"""

from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError

from smoke_optimiser.config import (
    DownwindConfig,
    OperationMode,
    ResolvedConfig,
    load_file_config,
    resolve_config,
    resolve_downwind_config,
)
from smoke_optimiser.downwind.command import run_downwind
from smoke_optimiser.optimiser.filters import apply_filters
from smoke_optimiser.optimiser.greedy import optimise
from smoke_optimiser.paths import resolve_project_paths_or_invocation_dir
from smoke_optimiser.profiler.models import ProfilingData
from smoke_optimiser.profiler.persistence import load_profile, save_profile
from smoke_optimiser.profiler.runner import (
    ProfilingIncompleteError,
    ProfilingUnavailableError,
    run_profiling,
)
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


def _load_profiling_data(profile_path: Path, project_offset: str) -> ProfilingData:
    """Load the profile the optimisation phase ranks, or explain its absence."""
    if not profile_path.exists():
        typer.secho(
            "❌ Error: No profiling data found. Run without --optimise-only first.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)

    return load_profile(profile_path, project_offset)


def _report_invalid_configuration(err: ValidationError) -> None:
    """Name every rejected setting, since both commands read the same table."""
    typer.secho("❌ Error: Invalid configuration in pyproject.toml", fg=typer.colors.RED, err=True)
    for error in err.errors():
        loc = ".".join(str(loc) for loc in error["loc"])
        typer.secho(f"  - {loc}: {error['msg']}", fg=typer.colors.RED, err=True)


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
def smoke(  # noqa: PLR0913 # special case for this function since Typer works this way
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
        bool | None,
        typer.Option(
            "--allow-ordered/--no-allow-ordered",
            help="Suppress error when pytest-randomly is not installed.",
        ),
    ] = None,
    src: Annotated[
        str | None,
        typer.Option("--src", help="Source directory/package for coverage instrumentation."),
    ] = None,
    iterations: Annotated[
        int | None,
        typer.Option("--iterations", help="Number of times to run the suite to average timing."),
    ] = None,
    allow_parallel_durations: Annotated[
        bool | None,
        typer.Option(
            "--allow-parallel-durations/--no-allow-parallel-durations",
            help="Build a smoke suite from a profile recorded with pytest-xdist, whose durations "
            "were measured under contention.",
        ),
    ] = None,
    profile_path: Annotated[
        Path | None,
        typer.Option("--profile-path", help="Path for the recorded profile, which downwind also reads."),
    ] = None,
) -> None:
    """Identify a minimal, high-value smoke test suite, and record the profile it came from."""
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
        "allow_parallel_durations": allow_parallel_durations,
        "profile_path": profile_path,
    }

    # Outside a git repository the repository root falls back to the invocation
    # directory. Sound, because a profile recorded there has no diff to be compared
    # against and no working-tree denominator -- its paths need only be consistent
    # with each other, and this command still ranks tests perfectly well.
    paths = resolve_project_paths_or_invocation_dir(Path.cwd())
    try:
        file_config = load_file_config(paths.invocation_dir)
        config = resolve_config(file_config, cli_overrides, paths.invocation_dir)
    except ValidationError as err:
        _report_invalid_configuration(err)
        raise typer.Exit(code=1) from None

    profiling_data = None
    profile_file = paths.invocation_dir / config.profile_path

    # Phase 1: Profiling
    if config.mode != OperationMode.OPTIMISE_ONLY:
        typer.secho("🔍 Running profiling...", fg=typer.colors.CYAN, bold=True)
        try:
            # Only the profile is wanted here, so pytest's exit code is dropped: this
            # command was asked to measure the suite, not to judge it, and a failing
            # test is still a profiled test. downwind, which runs the suite because
            # the developer needed it run, keeps the code instead.
            profiling_data = run_profiling(config.for_profiling(), paths).data
        except (ProfilingIncompleteError, ProfilingUnavailableError) as exc:
            typer.secho(f"❌ Error: {exc}", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1) from None
        save_profile(profiling_data, profile_file)

    # Phase 2: Optimisation
    if config.mode != OperationMode.PROFILE_ONLY:
        if profiling_data is None:
            profiling_data = _load_profiling_data(profile_file, paths.project_offset)

        _optimise_and_report(config, profiling_data)


@app.command()
def downwind(
    *,
    profile_path: Annotated[
        Path | None,
        typer.Option("--profile-path", help="Path of the profile to select from."),
    ] = None,
    downwind_file_path: Annotated[
        Path | None,
        typer.Option("--downwind-file-path", help="Path for the downwind selection file."),
    ] = None,
    pytest_args: Annotated[
        str | None,
        typer.Option("--pytest-args", help="Extra arguments forwarded to the downwind pytest run."),
    ] = None,
    regenerate_on_fallback: Annotated[
        bool | None,
        typer.Option(
            "--regenerate-on-fallback/--no-regenerate-on-fallback",
            help="Run a full-suite fallback under instrumentation, rewriting the profile it fell back from.",
        ),
    ] = None,
    src: Annotated[
        str | None,
        typer.Option(
            "--src",
            help="Source directory/package to instrument when a fallback regenerates the profile.",
        ),
    ] = None,
) -> None:
    """Run every test downwind of your working-tree changes, or the full suite when it cannot tell."""
    cli_overrides = {
        "profile_path": profile_path,
        "downwind_file_path": downwind_file_path,
        "downwind_pytest_args": pytest_args,
        "regenerate_on_fallback": regenerate_on_fallback,
        "cov_source": src,
    }

    invocation_dir = Path.cwd()
    try:
        config: DownwindConfig = resolve_downwind_config(
            load_file_config(invocation_dir), cli_overrides, invocation_dir
        )
    except ValidationError as err:
        _report_invalid_configuration(err)
        raise typer.Exit(code=1) from None

    raise typer.Exit(code=run_downwind(config, invocation_dir))


if __name__ == "__main__":
    app()
