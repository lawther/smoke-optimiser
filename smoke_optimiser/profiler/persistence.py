"""Read and write the profile file, the one artefact both commands share.

The profiling phase writes it; the optimiser ranks from it and downwind
selection answers from it. Both readers must reject a bad profile in exactly
the same words -- a schema mismatch, a corrupt file and a profile that cannot
say what it measured each need a different fix, and a second copy of these
messages would drift from the first.

Failures are reported and exit rather than raised, because every caller is a
CLI command whose only response is to say what went wrong and stop.
"""

import json
from pathlib import Path

import typer
from pydantic import ValidationError

from smoke_optimiser.profiler.models import (
    PROFILE_SCHEMA_VERSION,
    ImportEdgeModel,
    ImportGraphModel,
    MachineModel,
    ProfileSchemaMismatchError,
    ProfileScopeMissingError,
    ProfileScopeModel,
    ProfilingData,
    ProfilingDataFile,
    ProfilingMetaModel,
    ProfilingOutcomeModel,
    ReadObservationsModel,
    load_profiling_data_file,
)


def save_profile(profiling_data: ProfilingData, profile_path: Path) -> None:
    """Save profiling data to its JSON file."""
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
            files_read=list(po.files_read),
            directories_listed=list(po.directories_listed),
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
        scope=ProfileScopeModel.from_profile_scope(profiling_data.scope),
        unattributable_branches=list(profiling_data.unattributable_branches),
        reads=ReadObservationsModel.from_read_observations(profiling_data.reads),
        present_files=list(profiling_data.present_files),
    )
    profile_path.unlink(missing_ok=True)
    with profile_path.open("w") as f:
        json.dump(file_data.model_dump(mode="json"), f)
    typer.secho(f"💾 Profiling data saved to {profile_path}", fg=typer.colors.GREEN)


def load_profile(profile_path: Path) -> ProfilingData:
    """Load and validate the profile, or report why it cannot be used and exit.

    A profile that is present but unusable is a broken state rather than an
    absent one, so every case here stops the command. Callers that can carry
    on without a profile at all -- downwind, which falls back to the full
    suite -- check for the file themselves before calling.
    """
    try:
        with profile_path.open("rb") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        typer.secho(
            f"❌ Error: Failed to parse profiling data ({profile_path}): {e}",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1) from None

    try:
        return load_profiling_data_file(raw).to_profiling_data()
    except ProfileSchemaMismatchError as e:
        typer.secho(
            f"❌ Error: Profiling data ({profile_path}) has schema version {e.found!r}, but this build "
            f"expects schema version {e.expected}. Re-run the profiling phase to regenerate it.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1) from None
    except ProfileScopeMissingError as e:
        typer.secho(
            f"❌ Error: Profiling data ({profile_path}) is schema version {e.schema_version} but records "
            "no scope roots, so it cannot tell whether it has gone stale. Re-run the profiling phase; if the "
            "message persists, no coverage target or test path resolved inside the repository -- check --cov "
            "and pytest's testpaths.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1) from None
    except ValidationError as e:
        typer.secho(
            f"❌ Error: Failed to parse profiling data ({profile_path}): {e}",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1) from None
