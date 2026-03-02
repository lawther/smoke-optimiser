from pathlib import Path
from typing import Annotated

import typer

from smoke_optimiser.config import load_file_config, resolve_config

app = typer.Typer(pretty_exceptions_show_locals=False)


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
    smoke_file_path: Annotated[
        Path | None,
        typer.Option("--smoke-file-path", help="Location of the smoke suite file."),
    ] = None,
) -> None:
    """smoke-optimiser: Identify a minimal, high-value smoke test suite."""
    if profile_only and optimise_only:
        typer.echo("Error: --profile-only and --optimise-only are mutually exclusive.", err=True)
        raise typer.Exit(code=1)

    # Collect CLI overrides
    cli_overrides = {
        "profile_only": profile_only,
        "optimise_only": optimise_only,
        "time_cap": time_cap,
        "target_cov": target_cov,
        "include_mandatory": include,
        "exclude_mandatory": exclude,
        "pytest_args": pytest_args,
        "output_json": output_json,
        "allow_ordered": allow_ordered,
        "smoke_file_path": smoke_file_path,
    }

    file_config = load_file_config(Path.cwd())
    resolved = resolve_config(file_config, cli_overrides)

    typer.echo(f"Mode: {resolved.mode.value}")
    typer.echo(f"Time cap: {resolved.time_cap}s")
    typer.echo(f"Target coverage: {resolved.target_cov}%")


if __name__ == "__main__":
    app()
