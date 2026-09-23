"""Read and write the profile file, the one artefact both commands share.

The profiling phase writes it; the optimiser ranks from it and downwind
selection answers from it. Both readers must reject a bad profile in exactly
the same words -- a schema mismatch, a corrupt file and a profile that cannot
say what it measured each need a different fix, and a second copy of these
messages would drift from the first.

Every failure is rendered once, here, and reaches its caller as a
:class:`ProfileUnusableError` carrying both the finished message and a
:class:`ProfileFault` saying what KIND of broken it is. The kind matters
because the two callers respond differently: ``smoke`` reports and stops
whatever the fault, while downwind decides from it whether to regenerate over
the file, keep a copy of it first, or refuse.
"""

import json
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path

import typer
from pydantic import ValidationError

from smoke_optimiser.profiler.models import (
    PROFILE_SCHEMA_VERSION,
    ImportEdgeModel,
    ImportGraphModel,
    MachineModel,
    ProfileAnchorModel,
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
        iterations=profiling_data.meta.iterations,
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
        anchor=ProfileAnchorModel.from_profile_anchor(profiling_data.anchor),
        unattributable_branches=list(profiling_data.unattributable_branches),
        reads=ReadObservationsModel.from_read_observations(profiling_data.reads),
        present_files=list(profiling_data.present_files),
    )
    # Written beside the target and moved into place, because the runs most
    # likely to be killed are the long ones, and a write killed part-way must
    # not take the previous profile with it. os.replace is atomic, so the path
    # is never absent or half-written.
    staged = profile_path.with_name(profile_path.name + ".tmp")
    with staged.open("w") as f:
        json.dump(file_data.model_dump(mode="json"), f)
    staged.replace(profile_path)
    typer.secho(f"💾 Profiling data saved to {profile_path}", fg=typer.colors.GREEN)


class ProfileFault(Enum):
    """What kind of unusable a profile is, which decides who can repair it.

    OUTDATED, MISPLACED and UNREADABLE all mean "this file cannot be used and a
    fresh profile would be". They are told apart because each needs different
    words: the reader who has to act on the message cannot tell a stale build
    from a stale directory from a corrupt file by the symptom. MISCONFIGURED is
    the one a fresh profile would not repair.
    """

    OUTDATED = auto()
    """Written by a different build of the tool. Regenerating is the whole fix."""

    MISPLACED = auto()
    """Recorded from a different directory of this repository, so every path in it
    means something other than what it says. Perfectly valid and perfectly current;
    it simply describes a path space this run is not standing in. Regenerating from
    here is the whole fix."""

    UNREADABLE = auto()
    """Corrupt or invalid. Since the write became atomic no run of ours can leave
    one behind, so it points at something outside the tool or a bug inside it, and
    the file is evidence either way."""

    MISCONFIGURED = auto()
    """Names something wrong with the project's configuration rather than with the
    file. Regenerating reproduces it exactly, so only a human can clear it."""


@dataclass(frozen=True)
class ProfileUnusableError(Exception):
    """A profile that is present and cannot be used, with the message to say so.

    The message is rendered here rather than by the caller so that both callers
    say the same thing about the same file: two copies would drift, and each of
    these faults needs a different fix that the user has to be told exactly once.
    """

    fault: ProfileFault
    message: str


def read_profile(profile_path: Path, project_offset: str) -> ProfilingData:
    """Load and validate the profile, or raise saying which kind of broken it is.

    ``project_offset`` is where the reading run stands inside the repository.
    A profile recorded somewhere else describes a path space this run is not in,
    which is a fault about the file rather than about the project -- so it is
    reported as OUTDATED and the ordinary fallback rebuilds it.
    """
    try:
        with profile_path.open("rb") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        raise ProfileUnusableError(
            fault=ProfileFault.UNREADABLE,
            message=f"Failed to parse profiling data ({profile_path}): {e}",
        ) from None

    try:
        profile = load_profiling_data_file(raw).to_profiling_data()
    except ProfileSchemaMismatchError as e:
        rerun = (
            f" Re-run this command to regenerate it:\n\n  {e.command}\n"
            if e.command
            else " Re-run the profiling phase to regenerate it."
        )
        raise ProfileUnusableError(
            fault=ProfileFault.OUTDATED,
            message=(
                f"Profiling data ({profile_path}) has schema version {e.found!r}, but this build "
                f"expects schema version {e.expected}.{rerun}"
            ),
        ) from None
    except ProfileScopeMissingError as e:
        raise ProfileUnusableError(
            fault=ProfileFault.MISCONFIGURED, message=_no_scope_message(profile_path, e)
        ) from None
    except ValidationError as e:
        raise ProfileUnusableError(
            fault=ProfileFault.UNREADABLE,
            message=f"Failed to parse profiling data ({profile_path}): {e}",
        ) from None

    if profile.anchor.project_offset != project_offset:
        raise ProfileUnusableError(
            fault=ProfileFault.MISPLACED,
            message=_wrong_anchor_message(profile_path, profile.anchor.project_offset, project_offset),
        )
    return profile


def _wrong_anchor_message(profile_path: Path, recorded: str, current: str) -> str:
    """Name the mismatch, because every other symptom of it looks like success.

    Its paths are repository-relative, so a profile from another subdirectory
    parses perfectly and simply matches nothing -- the selection comes back
    empty and reads as a change no test reaches. Said outright here instead.
    """
    return (
        f"Profiling data ({profile_path}) was recorded from {recorded} and this run is in {current}, "
        "so every path in it means something other than what it says. Regenerating from here is the fix."
    )


def _no_scope_message(profile_path: Path, error: ProfileScopeMissingError) -> str:
    """Name the misconfiguration, and the three edits that clear it.

    Spelled out at length because this is the one fault regenerating cannot fix:
    a fresh profile is captured under the same configuration and comes out just
    as scope-less, so a message that only said "re-run the profiling phase" would
    send the user round a loop that never terminates.
    """
    return (
        f"Profiling data ({profile_path}) is schema version {error.schema_version} but records no scope "
        "roots, so it cannot tell whether it has gone stale -- and regenerating it would produce another "
        "one exactly like it.\n"
        "   Scope is the coverage targets plus pytest's test paths, so an empty scope means neither "
        "resolved to a path inside this repository. To fix, in order of likelihood:\n"
        "     1. Profile from the directory holding the project's pyproject.toml.\n"
        "     2. Point --cov at a directory in the tree rather than at an installed package:\n"
        '        [tool.smoke_optimiser] cov_source = "your_package"   (or --src=your_package)\n'
        "     3. Give pytest a test path inside the repository:\n"
        '        [tool.pytest.ini_options] testpaths = ["tests"]\n'
        "   Then regenerate: smoke-optimiser smoke --profile-only"
    )


def load_profile(profile_path: Path, project_offset: str) -> ProfilingData:
    """Load and validate the profile, or report why it cannot be used and exit.

    A profile that is present but unusable is a broken state rather than an
    absent one, so every case here stops the command. Callers that can do
    something better than stop -- downwind, which can rebuild the file it could
    not read -- call :func:`read_profile` and decide from the fault.
    """
    try:
        return read_profile(profile_path, project_offset)
    except ProfileUnusableError as e:
        typer.secho(f"\u274c Error: {e.message}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from None
