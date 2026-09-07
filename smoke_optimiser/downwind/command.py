"""From a git diff to a pytest run: the wiring the downwind command is.

Every decision this makes belongs to something else. The changed set comes
from :mod:`smoke_optimiser.downwind.changes`, the answer from
:mod:`smoke_optimiser.downwind.rules`, membership of the tree from
:func:`smoke_optimiser.profiler.scope.files_in_scope`, and the filtering from
the pytest plugin. What lives here is the order they happen in, the two
queries only this layer can make -- git and the filesystem -- and the report
a developer reads when the tool declines to select.

IT REFUSES TO RUN OUTSIDE THE REPOSITORY ROOT. git reports repo-relative
paths and the profile's paths are relative to the directory it was profiled
from, so the two agree only where those coincide. Assuming it would mean a
mismatch selecting nothing for a file plenty of tests depend on, and looking
perfectly healthy doing it -- so the requirement is checked rather than
hoped for. so-746 removes the restriction by making the profile's paths
repo-relative outright.

THE FULL SUITE IS STILL RUN THROUGH ``--downwind``. A refusal writes a
selection file carrying its blind spots and no node ids; the plugin then
leaves collection untouched and states the reasons in pytest's own report
header. One invocation path, and the reason travels with the run rather than
only with our console output. The one exception is a profile that does not
exist at all, where there is nothing to write a selection file about.
"""

from __future__ import annotations

import shlex
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import typer

from smoke_optimiser.downwind.blind_spots import BlindSpotReason, in_report_order
from smoke_optimiser.downwind.changes import (
    GitStatusError,
    changed_files,
    repository_root,
    tracked_files,
)
from smoke_optimiser.downwind.maps import DownwindMaps
from smoke_optimiser.downwind.rules import DownwindRefusal, downwind_of
from smoke_optimiser.profiler.persistence import load_profile
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
"""What rebuilds the profile. Named wherever a message says the profile is the problem."""


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


def _report_wrong_directory(invocation_dir: Path, repo_root: Path) -> None:
    """Refuse a subdirectory, and say why the answer would otherwise be wrong."""
    typer.secho(
        f"❌ Error: downwind must run from the repository root, not {invocation_dir}.",
        fg=typer.colors.RED,
        err=True,
    )
    typer.secho(
        "   git reports paths relative to the repository root while the profile's paths are\n"
        "   relative to where it was profiled from, so run it anywhere else and the two stop\n"
        "   agreeing -- selecting nothing for a file plenty of tests depend on, and looking\n"
        f"   perfectly healthy doing it.\n   Hint: cd {repo_root}",
        fg=typer.colors.RED,
        err=True,
    )


_EXPLANATIONS: dict[BlindSpotReason, str] = {
    BlindSpotReason.UNKNOWN_PATH: "the profile has never seen this file, so nothing is known to reach it",
    BlindSpotReason.NON_PYTHON_FILE: "not a Python file, and the maps only record Python execution",
    BlindSpotReason.UNATTRIBUTED_IMPORT: "nothing was seen to import this, so its dependents are unknown",
    BlindSpotReason.EXPIRED_PROFILE: "exists now but is absent from the profile, which is therefore out of date",
    BlindSpotReason.CHANGED_CONFTEST: "a changed conftest.py can affect any test it applies to",
    BlindSpotReason.TERMINAL_DEAD_END: "reached by the change, but it leads to no test and nothing imports it",
}
"""Why each per-file rule refused, in words the developer can act on.

When the tool declines to select, the reason IS the product: a bare enum
value would leave them with a slow run and no idea what to do about it.
RESOLUTION_ERRORS is absent because it names no file and so cannot be
rendered as "<file>: <clause>".
"""


def _describe(blind_spot: BlindSpot) -> str:
    """One blind spot as a line, naming the input it could not answer for."""
    if blind_spot.reason is BlindSpotReason.RESOLUTION_ERRORS:
        plural = "" if blind_spot.resolution_errors == 1 else "s"
        return (
            f"the import graph is missing {blind_spot.resolution_errors} edge{plural} the tracer could not "
            "record, so every closure it reports may be short"
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


def _select(profile: ProfilingData, repo_root: Path) -> _Selection:
    """Ask the rules, having made the two queries only this layer can make."""
    maps = DownwindMaps.from_profile(profile)
    changed = changed_files(repo_root)
    # The tree, not the diff, and narrowed by the predicate the profile was
    # captured under. Applying anything less than the whole predicate would
    # compare the maps against files they could never have contained.
    existing = files_in_scope(tracked_files(repo_root), profile.scope)

    answer = downwind_of(maps, changed, existing)
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


def _run_pytest(repo_root: Path, extra_args: list[str]) -> int:
    """Run pytest and hand back its exit code untouched.

    Untouched because this command gates a commit: a collection error, a
    usage error and a failing test must all keep the non-zero code that
    stops the commit, and none of them may be reinterpreted as a successful
    selective run.
    """
    command = [sys.executable, "-m", "pytest", *extra_args]
    typer.secho(f"🏃 {' '.join(command[2:])}", fg=typer.colors.CYAN)
    # Built from sys.executable plus the user's own configured arguments.
    return subprocess.run(command, cwd=repo_root, check=False).returncode  # noqa: S603


def run_downwind(config: DownwindConfig, invocation_dir: Path) -> int:
    """Select the tests downwind of the working-tree diff and run them.

    Returns the exit code the process should use: pytest's own wherever
    pytest ran, and 1 for the failures that stop it running at all.
    """
    try:
        repo_root = repository_root(invocation_dir)
    except GitStatusError as exc:
        _report_git_failure(exc)
        return EXIT_ERROR

    if repo_root.resolve() != invocation_dir.resolve():
        _report_wrong_directory(invocation_dir, repo_root)
        return EXIT_ERROR

    extra_args = shlex.split(config.pytest_args)

    if not config.profile_path.exists():
        typer.secho(
            f"⚠️ Warning: no profile at {config.profile_path}, so there is nothing to select from. "
            f"Running the full suite.\n   To select next time, record one: {REGENERATE_COMMAND}",
            fg=typer.colors.YELLOW,
            err=True,
        )
        return _run_pytest(repo_root, extra_args)

    profile = load_profile(config.profile_path)

    try:
        selection = _select(profile, repo_root)
    except GitStatusError as exc:
        _report_git_failure(exc)
        return EXIT_ERROR

    _write_selection(selection, profile, config)

    if selection.refused:
        _report_refusal(selection, config)
    elif not selection.node_ids:
        # Nothing to filter to, so pytest would collect nothing and exit 5 --
        # a failed commit for a change that correctly has nothing to run.
        _report_nothing_downwind(selection)
        return 0
    else:
        _report_selection(selection)

    return _run_pytest(
        repo_root,
        ["--downwind", f"--downwind-file-path={config.downwind_file_path}", *extra_args],
    )
