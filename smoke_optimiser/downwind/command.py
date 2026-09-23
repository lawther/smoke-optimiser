"""From a git diff to a pytest run: the wiring the downwind command is.

Every decision this makes belongs to something else. The changed set comes
from :mod:`smoke_optimiser.downwind.changes`, the answer from
:mod:`smoke_optimiser.downwind.rules`, membership of the tree from
:func:`smoke_optimiser.profiler.scope.files_in_scope`, and the filtering from
the pytest plugin. What lives here is the order they happen in, the two
queries only this layer can make -- git and the filesystem -- and the report
a developer reads when the tool declines to select.

IT RUNS ANYWHERE INSIDE THE REPOSITORY. git reports repo-relative paths and
so does the profile, so the two agree wherever the command was invoked. What
the invocation directory decides is something else: which pyproject.toml
supplies the configuration, where the profile and selection file live, and the
cwd pytest itself is given -- so a project in a subdirectory of a larger repo
gets its own rootdir, testpaths and pythonpath. Only git's absence is refused,
because without a diff there is nothing to select from.

THE FULL SUITE IS STILL RUN THROUGH ``--downwind``. A refusal writes a
selection file carrying its blind spots and no node ids; the plugin then
leaves collection untouched and states the reasons in pytest's own report
header. One invocation path, and the reason travels with the run rather than
only with our console output. The one exception is a profile that does not
exist at all, where there is nothing to write a selection file about.

A FULL-SUITE FALLBACK RUNS UNDER PROFILING INSTRUMENTATION AND REWRITES THE
PROFILE IT FELL BACK FROM. The run that pays for the fallback is the run that
repairs the map, which is what stops staleness being a ratchet: without it the
map expires, every commit pays a full suite, and it keeps paying until someone
remembers to regenerate by hand. Nothing detaches and nothing fires later --
the only suite that runs is the one the developer was already waiting on.

The rewrite is CONDITIONAL, because a partial profile is worse than a stale
one: it reads as fact rather than as absence, and a file no test appears to
reach selects nothing. Every completeness check the profiling path already
makes still applies here, and any of them failing leaves the previous profile
exactly where it was and says so. A fallback whose TESTS fail is not one of
those cases -- outcomes record which tests failed, and the map is as good
either way -- so the rewrite turns on the data being complete, never on the
suite being green.
"""

from __future__ import annotations

import shlex
import subprocess
import sys
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import typer

from smoke_optimiser.config import CovSourceOrigin, ProfilingRunConfig
from smoke_optimiser.downwind.blind_spots import BlindSpotReason, in_report_order
from smoke_optimiser.downwind.changes import (
    GitStatusError,
    changed_files,
    tracked_files,
)
from smoke_optimiser.downwind.maps import DownwindMaps
from smoke_optimiser.downwind.rules import DownwindRefusal, downwind_of
from smoke_optimiser.paths import ProjectPaths, resolve_project_paths
from smoke_optimiser.profiler.persistence import (
    ProfileFault,
    ProfileUnusableError,
    read_profile,
    save_profile,
)
from smoke_optimiser.profiler.runner import (
    ProfilingIncompleteError,
    ProfilingUnavailableError,
    run_profiling,
)
from smoke_optimiser.profiler.scope import files_in_scope
from smoke_optimiser.reports.downwind_suite import (
    BlindSpotModel,
    DownwindSuiteFile,
    ProfileIdentityModel,
    write_downwind_suite,
)

if TYPE_CHECKING:
    from pathlib import Path

    from smoke_optimiser.config import DownwindConfig
    from smoke_optimiser.downwind.blind_spots import BlindSpot
    from smoke_optimiser.downwind.changes import ChangedFile
    from smoke_optimiser.profiler.models import ProfilingData

EXIT_ERROR = 1
"""Our own failures. pytest's exit code is passed through untouched otherwise."""

REGENERATE_COMMAND = "smoke-optimiser smoke --profile-only"
"""What rebuilds the profile by hand. Named where a message says the profile is the
problem and this run is not going to fix it -- when regeneration is switched off, or
when it was tried and did not produce anything trustworthy."""

CORRUPT_PROFILE_SUFFIX = ".corrupt"
"""Where an unreadable profile is kept when a fallback replaces it.

The write became atomic in so-n6b.46, so no run of ours can leave a half-written
profile behind any more: one that will not parse points at something outside the
tool or at a bug inside it, and either way the file is the only evidence of it.
Regenerating over the top would destroy that, so it is moved rather than
overwritten."""


class _DownwindHaltError(Exception):
    """Raised, already reported, by anything that stops the command before pytest.

    Carries nothing: every site that raises it has already said what went wrong,
    in the words that fit that particular failure. It exists so those sites can be
    functions rather than early returns, and so ``run_downwind`` names the one
    exit code they all share exactly once.
    """


@dataclass(frozen=True)
class _Selection:
    """What the rules answered, plus what the report needs to say it in context.

    ``profile_test_count`` is the denominator: a selection means nothing to a
    reader without knowing what it was selected out of.
    """

    node_ids: frozenset[str]
    blind_spots: frozenset[BlindSpot]
    changed: frozenset[ChangedFile]
    profile_test_count: int

    @property
    def refused(self) -> bool:
        return bool(self.blind_spots)


def _report_git_failure(exc: GitStatusError) -> None:
    """Say what was tried, where, and what git said about it.

    git's own stderr is reproduced verbatim: it distinguishes "not a
    repository" from a broken index or a permissions problem far better than
    anything this layer could infer from a return code.
    """
    typer.secho("❌ Error: could not read the repository state from git.", fg=typer.colors.RED, err=True)
    typer.secho(f"     tried:     {exc.command} in {exc.directory}", fg=typer.colors.RED, err=True)
    typer.secho(f"     git said:  {exc.detail}", fg=typer.colors.RED, err=True)
    typer.secho(
        "   downwind selects from your working-tree diff, so with no diff to read there is\n"
        "   nothing it can honestly select. Run it from inside a git repository, or use\n"
        "   `pytest` directly to run the whole suite.",
        fg=typer.colors.RED,
        err=True,
    )


_EXPLANATIONS: dict[BlindSpotReason, str] = {
    BlindSpotReason.UNKNOWN_PATH: "the profile has never seen this file, so nothing is known to reach it",
    BlindSpotReason.UNATTRIBUTED_IMPORT: "nothing was seen to import this, so its dependents are unknown",
    BlindSpotReason.UNATTRIBUTED_READ: (
        "read while the suite was starting up rather than by any test, so what depends on it is unknown"
    ),
    BlindSpotReason.ENVIRONMENT_FILE: (
        "defines the environment every test runs in, which no map can measure the reach of"
    ),
    BlindSpotReason.EXPIRED_PROFILE: "exists now but is absent from the profile, which is therefore out of date",
    BlindSpotReason.CHANGED_CONFTEST: "a changed conftest.py can affect any test it applies to",
    BlindSpotReason.TERMINAL_DEAD_END: "reached by the change, but it leads to no test and nothing imports it",
}
"""Why each per-file rule refused, in words the developer can act on.

When the tool declines to select, the reason IS the product: a bare enum
value would leave them with a slow run and no idea what to do about it.
RESOLUTION_ERRORS and READ_ERRORS are absent because they name no file and so
cannot be rendered as "<file>: <clause>".
"""


def _describe(blind_spot: BlindSpot) -> str:
    """One blind spot as a line, naming the input it could not answer for."""
    if blind_spot.reason is BlindSpotReason.RESOLUTION_ERRORS:
        plural = "" if blind_spot.resolution_errors == 1 else "s"
        return (
            f"the import graph is missing {blind_spot.resolution_errors} edge{plural} the tracer could not "
            "record, so every closure it reports may be short"
        )
    if blind_spot.reason is BlindSpotReason.READ_ERRORS:
        plural = "" if blind_spot.read_errors == 1 else "s"
        return (
            f"the file read map is missing {blind_spot.read_errors} read{plural} the tracer could not "
            "record, so what a changed data file reaches may be under-reported"
        )
    return f"{blind_spot.file}: {_EXPLANATIONS[blind_spot.reason]}"


def _report_refusal(selection: _Selection, config: DownwindConfig) -> None:
    """Print the blind spots, in the one order they are ever rendered in."""
    typer.secho(
        f"⚠️ {len(selection.blind_spots)} blind spots across {len(selection.changed)} changed files, "
        "so the full suite runs:",
        fg=typer.colors.YELLOW,
        err=True,
    )
    for blind_spot in in_report_order(selection.blind_spots):
        typer.secho(f"     {_describe(blind_spot)}", fg=typer.colors.YELLOW, err=True)
    typer.secho(
        f"   Selection written to {config.downwind_file_path}.",
        fg=typer.colors.YELLOW,
        err=True,
    )


def _report_nothing_downwind(selection: _Selection) -> None:
    """Zero tests is a real answer, and which of its two shapes matters.

    An empty changed set is unremarkable. A non-empty one the profile says no
    test reaches is a finding about the codebase -- you changed code nothing
    tests -- and is the one shape that can also mean the profile is missing a
    route to the suite, so it names the files and how to rebuild.
    """
    if not selection.changed:
        typer.secho("✅ No changes in the working tree, so nothing is downwind. pytest not run.", fg=typer.colors.GREEN)
        return

    typer.secho(
        f"⚠️ Warning: {len(selection.changed)} changed files, and the profile says no test reaches them:",
        fg=typer.colors.YELLOW,
        err=True,
    )
    for path in sorted(changed.path for changed in selection.changed):
        typer.secho(f"     {path}", fg=typer.colors.YELLOW, err=True)
    typer.secho(
        "   Nothing was run. If that is wrong, the profile is missing a route to the suite --\n"
        f"   regenerate it: {REGENERATE_COMMAND}",
        fg=typer.colors.YELLOW,
        err=True,
    )
    typer.secho(
        f"✅ 0 of {selection.profile_test_count} tests downwind. pytest not run.",
        fg=typer.colors.GREEN,
    )


def _report_selection(selection: _Selection) -> None:
    """Say what was considered changed and how much of the suite that comes to."""
    typer.secho(
        f"🎯 {len(selection.node_ids)} of {selection.profile_test_count} tests downwind of "
        f"{len(selection.changed)} changed files.",
        fg=typer.colors.CYAN,
        bold=True,
    )
    for path in sorted(changed.path for changed in selection.changed):
        typer.secho(f"     {path}", fg=typer.colors.CYAN)


def _drop_own_artefacts(
    changed: frozenset[ChangedFile], repo_root: Path, config: DownwindConfig
) -> frozenset[ChangedFile]:
    """Exclude smoke-optimiser's own generated files from the changed set.

    The profile and the selection file are rewritten by this tool's own runs, not by anything a
    developer did to the tree -- so a project that has not gitignored them would otherwise see
    every downwind run force the full suite over changes it made to itself the run before.

    The two sides live in DIFFERENT path spaces, and that is the point. ``config``'s paths
    are already absolute by the time they reach here, anchored on the invocation directory
    where the pyproject.toml that names them is; ``changed`` reports paths relative to the
    repository root, as git does. Resolving both to absolute paths is what lets them be
    compared at all -- anchor the artefacts on the repository root while they actually live
    in a subdirectory and the comparison never matches, so the profile this tool just
    rewrote reads as a working-tree change on the next run, every run refuses, and every
    refusal rewrites it again.
    """
    profile = config.profile_path.resolve()
    own_artefacts = {
        profile,
        # A fallback that rebuilt an unreadable profile leaves this beside it, and a
        # project's gitignore names the profile rather than its siblings -- so without
        # this the run that repaired the map creates a permanent untracked change and
        # every run after it falls back again.
        profile.with_name(profile.name + CORRUPT_PROFILE_SUFFIX),
        config.downwind_file_path.resolve(),
    }
    return frozenset(file for file in changed if (repo_root / file.path).resolve() not in own_artefacts)


def _select(profile: ProfilingData, paths: ProjectPaths, config: DownwindConfig) -> _Selection:
    """Ask the rules, having made the two queries only this layer can make."""
    maps = DownwindMaps.from_profile(profile)
    changed = _drop_own_artefacts(changed_files(paths.repo_root), paths.repo_root, config)
    # The tree, not the diff, and narrowed by the predicate the profile was
    # captured under. Applying anything less than the whole predicate would
    # compare the maps against files they could never have contained.
    existing = files_in_scope(tracked_files(paths.repo_root), profile.scope)

    answer = downwind_of(maps, changed, existing, config.environment_files)
    refused = isinstance(answer, DownwindRefusal)
    return _Selection(
        node_ids=frozenset() if refused else answer.node_ids,
        blind_spots=answer.blind_spots if refused else frozenset(),
        changed=changed,
        profile_test_count=len(profile.tests),
    )


def _write_selection(selection: _Selection, profile: ProfilingData, config: DownwindConfig) -> None:
    """Write .downwind.json, in the one order a refusal is ever written in."""
    suite = DownwindSuiteFile(
        generated_at=datetime.now(UTC),
        changed_files=sorted(changed.path for changed in selection.changed),
        node_ids=sorted(selection.node_ids),
        profile=ProfileIdentityModel(commit=profile.meta.commit, timestamp=profile.meta.timestamp),
        blind_spots=[BlindSpotModel.from_blind_spot(spot) for spot in in_report_order(selection.blind_spots)],
    )
    write_downwind_suite(suite, config.downwind_file_path)


def _run_pytest(invocation_dir: Path, extra_args: list[str]) -> int:
    """Run pytest and hand back its exit code untouched.

    Untouched because this command gates a commit: a collection error, a
    usage error and a failing test must all keep the non-zero code that
    stops the commit, and none of them may be reinterpreted as a successful
    selective run.

    In the INVOCATION directory, not the repository root. pytest resolves its
    rootdir, ``testpaths`` and ``pythonpath`` from the pyproject.toml it finds,
    and the node ids in the selection file are rootdir-relative -- so running it
    anywhere else would hand it neither its configuration nor ids it recognises.
    """
    command = [sys.executable, "-m", "pytest", *extra_args]
    typer.secho(f"🏃 {' '.join(command[2:])}", fg=typer.colors.CYAN)
    # Built from sys.executable plus the user's own configured arguments.
    return subprocess.run(command, cwd=invocation_dir, check=False).returncode  # noqa: S603


def _report_absent_profile(config: DownwindConfig) -> None:
    """Say there is nothing to select from, and what this run does about it."""
    if config.regenerate_on_fallback:
        typer.secho(
            f"⚠️ Warning: no profile at {config.profile_path}, so there is nothing to select from. "
            "Running the full suite under instrumentation to record one, so the next run can select.",
            fg=typer.colors.YELLOW,
            err=True,
        )
        return
    typer.secho(
        f"⚠️ Warning: no profile at {config.profile_path}, so there is nothing to select from. "
        f"Running the full suite.\n   To select next time, record one: {REGENERATE_COMMAND}",
        fg=typer.colors.YELLOW,
        err=True,
    )


def _report_outdated_profile(config: DownwindConfig, message: str) -> None:
    """A profile from a different build of the tool: benign, and fixed by rebuilding."""
    if config.regenerate_on_fallback:
        typer.secho(
            f"⚠️ Warning: the profile at {config.profile_path} was written by a different build of "
            "smoke-optimiser, so it cannot be read. Running the full suite under instrumentation to replace it.",
            fg=typer.colors.YELLOW,
            err=True,
        )
        return
    typer.secho(f"❌ Error: {message}", fg=typer.colors.RED, err=True)


def _report_misplaced_profile(config: DownwindConfig, message: str) -> None:
    """A profile from elsewhere in the repository: benign, and fixed by rebuilding here."""
    if config.regenerate_on_fallback:
        typer.secho(
            f"⚠️ Warning: {message}\n   Running the full suite under instrumentation to replace it.",
            fg=typer.colors.YELLOW,
            err=True,
        )
        return
    typer.secho(f"❌ Error: {message}", fg=typer.colors.RED, err=True)


def _preserve_unreadable_profile(config: DownwindConfig, message: str) -> None:
    """Move an unreadable profile aside before a fallback writes over its path.

    Both halves matter. The developer is unblocked, because the fallback rebuilds
    the file either way; and the broken one survives, because it is the only thing
    anybody could look at to find out how it got that way.
    """
    if not config.regenerate_on_fallback:
        typer.secho(f"❌ Error: {message}", fg=typer.colors.RED, err=True)
        return

    kept = config.profile_path.with_name(config.profile_path.name + CORRUPT_PROFILE_SUFFIX)
    try:
        config.profile_path.replace(kept)
    except OSError as exc:
        typer.secho(
            f"⚠️ Warning: the profile at {config.profile_path} could not be read, and could not be moved "
            f"aside either ({exc}). Running the full suite under instrumentation to replace it.",
            fg=typer.colors.YELLOW,
            err=True,
        )
        return

    typer.secho(
        f"⚠️ Warning: the profile at {config.profile_path} could not be read: {message}\n"
        f"   Kept a copy at {kept} -- please attach it if you report this.\n"
        "   Running the full suite under instrumentation to rebuild it.",
        fg=typer.colors.YELLOW,
        err=True,
    )


def _load_profile(config: DownwindConfig, project_offset: str) -> ProfilingData | None:
    """The profile to select from, or None when the run must fall back instead.

    None means "nothing to select from, and a fresh profile would fix that" --
    absent, from another build, or unreadable. The one fault that is not that
    shape raises, because no suite this command could run would clear it.
    """
    if not config.profile_path.exists():
        _report_absent_profile(config)
        return None

    try:
        return read_profile(config.profile_path, project_offset)
    except ProfileUnusableError as exc:
        if exc.fault is ProfileFault.MISCONFIGURED:
            # The one fault a fallback cannot repair: regenerating produces another
            # profile with the same fault, so a run that "repaired" it would leave
            # every commit paying a full instrumented suite for ever, silently.
            typer.secho(f"❌ Error: {exc.message}", fg=typer.colors.RED, err=True)
            raise _DownwindHaltError from None
        if exc.fault is ProfileFault.UNREADABLE:
            _preserve_unreadable_profile(config, exc.message)
        elif exc.fault is ProfileFault.MISPLACED:
            _report_misplaced_profile(config, exc.message)
        else:
            _report_outdated_profile(config, exc.message)
        return None


DISTRIBUTION_FLAGS: frozenset[str] = frozenset({"-n", "--numprocesses", "--dist", "--maxprocesses"})
"""pytest-xdist flags the instrumented fallback keeps from the downwind arguments.

Everything else in those arguments is dropped, because anything that changes WHICH
tests run would profile a fraction of the suite. These change only how the same
tests are spread over processes, so the profile is of the whole suite either way --
and dropping them would make the fallback run serially a suite the developer runs
in parallel, which is a far bigger cost than the +11% instrumentation itself.

What it costs is the durations: measured under contention, they rank badly, so
``smoke`` refuses to build a suite from such a profile without
--allow-parallel-durations. The maps downwind selects from are unaffected.
"""

# Explicit statements of what to measure. Both beat reproducing whatever the profile
# being replaced happened to record, because a project that says what it covers has
# answered the question, and a profile is only ever evidence of an earlier answer.
_EXPLICIT_ORIGINS = frozenset({CovSourceOrigin.CONFIGURED, CovSourceOrigin.COVERAGE_CONFIG})


def _parallelism_args(pytest_args: str) -> list[str]:
    """The distribution flags out of the downwind arguments, values included."""
    tokens = shlex.split(pytest_args)
    kept: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        flag = token.split("=", maxsplit=1)[0]
        if flag in DISTRIBUTION_FLAGS:
            # "-n auto" carries its value in the next token; "--dist=worksteal" does not.
            takes_next = "=" not in token and index + 1 < len(tokens)
            kept.extend(tokens[index : index + 2] if takes_next else [token])
            index += 2 if takes_next else 1
            continue
        index += 1
    return kept


def _coverage_source(config: DownwindConfig, replaced: ProfilingData | None) -> ProfilingRunConfig:
    """What the fallback instruments, preferring the least speculative answer available.

    An explicit statement wins. Failing that, the coverage root the profile being
    replaced was recorded under, so a fallback reproduces the profile it is
    replacing rather than quietly substituting a different one -- the failure this
    exists to prevent is a run that widened a package to the whole repository and
    put every unmeasurable file in the tree into scope. Only when there is neither
    does it fall through to the guesses, which announce themselves.

    A profile recording several coverage roots is left to the guesses, since only
    one can be passed as --src; the profile itself says which ones, and naming one
    of them would be its own silent substitution.
    """
    profiling = config.profiling
    if profiling.cov_source_origin in _EXPLICIT_ORIGINS or replaced is None:
        return profiling
    if len(replaced.scope.coverage_roots) == 1:
        return ProfilingRunConfig(
            cov_source=next(iter(replaced.scope.coverage_roots)),
            cov_source_origin=CovSourceOrigin.REPLACED_PROFILE,
            pytest_args=profiling.pytest_args,
            allow_ordered=profiling.allow_ordered,
            iterations=profiling.iterations,
        )
    return profiling


def _profiling_args(config: DownwindConfig, downwind_args: list[str]) -> str:
    """The pytest arguments the instrumented fallback runs under.

    The PROFILING arguments, not the downwind ones. The profile a fallback writes
    has to be the profile ``smoke --profile-only`` would have written, and a
    per-commit ``-x``, ``-k`` or ``--lf`` in the downwind arguments would narrow
    collection into a profile of part of the suite -- one that every completeness
    check here passes, because the hook still writes every artefact and the
    coverage contexts still agree with the outcomes. Silently profiling a fraction
    of the suite is precisely the partial overwrite this path exists to prevent.

    The exception is the distribution flags, which cannot narrow anything: see
    :data:`DISTRIBUTION_FLAGS`.

    The downwind flags themselves are appended, so the plugin still states in
    pytest's own header why the full suite is running.
    """
    kept = [
        config.profiling.pytest_args,
        *(shlex.quote(arg) for arg in _parallelism_args(config.pytest_args)),
        *(shlex.quote(arg) for arg in downwind_args),
    ]
    return " ".join(part for part in kept if part).strip()


def _report_failed_regeneration(config: DownwindConfig, message: str) -> None:
    """Say that the suite ran, the profile did not change, and which one still stands."""
    typer.secho(
        "⚠️ Warning: the suite ran, but it did not produce a profile that can be trusted, so the profile "
        f"at {config.profile_path} is unchanged.\n   {message}",
        fg=typer.colors.YELLOW,
        err=True,
    )


def _regenerate(
    config: DownwindConfig,
    paths: ProjectPaths,
    downwind_args: list[str],
    extra_args: list[str],
    replaced: ProfilingData | None,
) -> int:
    """Run the full suite instrumented, and rewrite the profile if it came out whole.

    Returns the exit code the command should use: pytest's own wherever the suite
    reached a verdict, because this command gates a commit and the tests are what
    is being judged; 1 where the suite passed and left no usable profile, so a
    regeneration that quietly failed cannot pass for an ordinary green run.
    """
    typer.secho(
        "🔁 Running the full suite under instrumentation, so this run rebuilds the map it fell back from.",
        fg=typer.colors.CYAN,
        bold=True,
    )
    kept = _parallelism_args(config.pytest_args)
    dropped = [arg for arg in extra_args if arg not in kept]
    if dropped:
        typer.secho(
            f"   Note: the downwind pytest arguments ({' '.join(dropped)}) are not applied to this run -- "
            "the profile it records has to be the one the profiling phase would have recorded.",
            fg=typer.colors.CYAN,
        )

    source = _coverage_source(config, replaced)
    profiling = ProfilingRunConfig(
        cov_source=source.cov_source,
        cov_source_origin=source.cov_source_origin,
        pytest_args=_profiling_args(config, downwind_args),
        allow_ordered=source.allow_ordered,
        iterations=source.iterations,
    )

    try:
        run = run_profiling(profiling, paths)
    except ProfilingUnavailableError as exc:
        # Nothing ran at all, so the tests the developer is waiting on still have to.
        typer.secho(
            f"⚠️ Warning: the suite could not be profiled ({exc}), so the profile was not regenerated. "
            f"Running the full suite without instrumentation.\n"
            f"   To rebuild the profile once that is fixed: {REGENERATE_COMMAND}",
            fg=typer.colors.YELLOW,
            err=True,
        )
        return _run_pytest(paths.invocation_dir, [*downwind_args, *extra_args])
    except ProfilingIncompleteError as exc:
        _report_failed_regeneration(config, str(exc))
        # A suite that reached a verdict keeps it: those tests really did fail, which
        # is a truer thing to say about the commit than our own failure to profile it.
        # A suite that passed reports 1 instead, so a regeneration that did not happen
        # is never invisible.
        return exc.returncode or EXIT_ERROR

    save_profile(run.data, config.profile_path)
    typer.secho(
        f"🧭 Profile rebuilt from this run, so the next one selects from {len(run.data.tests)} tests.",
        fg=typer.colors.GREEN,
    )
    return run.returncode


def _run_full_suite(
    config: DownwindConfig, paths: ProjectPaths, extra_args: list[str], replaced: ProfilingData | None
) -> int:
    """Run everything, instrumented wherever that is switched on.

    ``replaced`` is the profile this fallback is standing in for, where there was
    a readable one. It decides two things: whether there is a selection file for
    the plugin to read -- there is one exactly when the rules got far enough to
    refuse -- and what the instrumented run instruments, since reproducing the
    coverage root the old profile used beats guessing a new one.
    """
    downwind_args = ["--downwind", f"--downwind-file-path={config.downwind_file_path}"] if replaced is not None else []

    if not config.regenerate_on_fallback:
        return _run_pytest(paths.invocation_dir, [*downwind_args, *extra_args])

    return _regenerate(config, paths, downwind_args, extra_args, replaced)


def _anchored(config: DownwindConfig, invocation_dir: Path) -> DownwindConfig:
    """The same config with its artefact paths made absolute, once, here.

    Both are configured relative to the pyproject.toml that names them, which is
    the invocation directory's. Left relative they would be resolved against the
    process's cwd by every reader and against a root by every comparison -- and
    those agree only while the two coincide, which is the assumption this whole
    command has stopped making. Joining an absolute configured path is a no-op,
    so a project that spelled either one absolutely is unaffected.
    """
    return replace(
        config,
        profile_path=invocation_dir / config.profile_path,
        downwind_file_path=invocation_dir / config.downwind_file_path,
    )


def _resolve_paths(invocation_dir: Path) -> ProjectPaths:
    """Both roots, or a halt: without git there is no diff to select from.

    The invocation directory need only be INSIDE the repository, not equal to
    its root. What it must not be is outside a repository altogether, which is
    the one thing this refuses -- and it refuses rather than falling back to the
    full suite, because a selection command that cannot read the diff has been
    asked a question it has no way to answer.
    """
    try:
        return resolve_project_paths(invocation_dir)
    except GitStatusError as exc:
        _report_git_failure(exc)
        raise _DownwindHaltError from None


def run_downwind(config: DownwindConfig, invocation_dir: Path) -> int:
    """Select the tests downwind of the working-tree diff and run them.

    Returns the exit code the process should use: pytest's own wherever
    pytest ran, and 1 for the failures that stop it running at all.
    """
    try:
        paths = _resolve_paths(invocation_dir)
        config = _anchored(config, paths.invocation_dir)
        profile = _load_profile(config, paths.project_offset)
    except _DownwindHaltError:
        return EXIT_ERROR

    extra_args = shlex.split(config.pytest_args)

    if profile is None:
        return _run_full_suite(config, paths, extra_args, replaced=None)

    try:
        selection = _select(profile, paths, config)
    except GitStatusError as exc:
        _report_git_failure(exc)
        return EXIT_ERROR

    _write_selection(selection, profile, config)

    if selection.refused:
        _report_refusal(selection, config)
        return _run_full_suite(config, paths, extra_args, replaced=profile)

    if not selection.node_ids:
        # Nothing to filter to, so pytest would collect nothing and exit 5 --
        # a failed commit for a change that correctly has nothing to run.
        _report_nothing_downwind(selection)
        return 0

    _report_selection(selection)
    return _run_pytest(
        paths.invocation_dir,
        ["--downwind", f"--downwind-file-path={config.downwind_file_path}", *extra_args],
    )
