import tomllib
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, NamedTuple

import typer
from pydantic import BaseModel, Field, ValidationError

from smoke_optimiser.downwind.environment_files import DEFAULT_ENVIRONMENT_FILES
from smoke_optimiser.profiler.scope import WHOLE_REPOSITORY


class OperationMode(Enum):
    """Execution modes for smoke-optimiser."""

    FULL = "full"
    PROFILE_ONLY = "profile-only"
    OPTIMISE_ONLY = "optimise-only"


class CovSourceOrigin(Enum):
    """Where the coverage source came from.

    Carried alongside the value because what a profile instruments decides what
    it can ever know, and a value nobody chose must be able to be told from one
    somebody did. A run that silently widened the coverage root from a package
    to the whole repository is how a profile acquires files coverage can never
    measure, and with them a permanent expiry.
    """

    CONFIGURED = "configured"
    """Stated by the user, in [tool.smoke_optimiser] or on the command line."""

    COVERAGE_CONFIG = "coverage_config"
    """Taken from the project's own [tool.coverage.run] source or source_pkgs."""

    SRC_LAYOUT = "src_layout"
    """Guessed from a src/ directory at the repository root."""

    PROJECT_NAME = "project_name"
    """Guessed from a package named after the project in pyproject.toml."""

    REPLACED_PROFILE = "replaced_profile"
    """The coverage root the profile being regenerated was itself recorded under."""

    UNDISCOVERED = "undiscovered"
    """Nothing was configured and no heuristic matched, so nothing may be profiled."""


class DiscoveredCovSource(NamedTuple):
    """A coverage source and how it was arrived at.

    ``value`` is the whole repository when nothing was found, which is what
    ``--src`` would have to say to ask for that deliberately -- so the message
    that refuses to profile can offer the exact command that would.
    """

    value: str
    origin: CovSourceOrigin
    note: str = ""
    """Anything the user needs to know about the value beyond where it came from."""


class FileConfig(BaseModel):
    """Configuration model for [tool.smoke_optimiser] in pyproject.toml."""

    time_cap: float = Field(default=15.0, ge=0.0)
    target_cov: float = Field(default=100.0, ge=0.0, le=100.0)
    include_mandatory: list[str] = Field(default_factory=list)
    exclude_mandatory: list[str] = Field(default_factory=list)
    pytest_args: str = Field(default="")
    output_json: Path = Field(default=Path("./.smoke_suite.json"))
    allow_ordered: bool = Field(default=False)
    smoke_file_path: Path = Field(default=Path("./.smoke_suite.json"))
    cov_source: str | None = Field(default=None)
    iterations: int = Field(default=1, ge=1)
    allow_parallel_durations: bool = Field(default=False)
    profile_path: Path = Field(default=Path("./.smoke_profiling_data.json"))
    downwind_file_path: Path = Field(default=Path("./.downwind.json"))
    downwind_pytest_args: str = Field(default="")
    regenerate_on_fallback: bool = Field(default=True)
    extra_environment_files: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class ProfilingRunConfig:
    """What the profiling run itself needs, and nothing else.

    Named separately because both commands record profiles and they disagree
    about only one of these settings. ``smoke`` profiles to rank tests, so it
    honours the project's ``iterations``; the downwind fallback profiles to
    repair the map, which every iteration produces identically -- the maps
    merge where the durations average -- so a second pass would cost the
    developer another full suite for nothing they are waiting on.

    Passing a whole :class:`ResolvedConfig` to the runner would hand it a mode,
    a time cap, a coverage target and an output path it never reads, and would
    leave the downwind command manufacturing all four to reach the four it
    means.

    ``cov_source_origin`` travels with the value because the runner has to say
    where it came from before it instruments anything, and has to refuse
    outright when the answer is that nobody chose it.
    """

    cov_source: str
    cov_source_origin: CovSourceOrigin
    pytest_args: str
    allow_ordered: bool
    iterations: int


@dataclass(frozen=True)
class ResolvedConfig:
    """Fully resolved configuration after merging defaults, file, and CLI."""

    mode: OperationMode
    time_cap: float
    target_cov: float
    include_mandatory: list[str]
    exclude_mandatory: list[str]
    pytest_args: str
    output_json: Path
    allow_ordered: bool
    cov_source: str
    cov_source_origin: CovSourceOrigin
    iterations: int
    allow_parallel_durations: bool
    profile_path: Path

    def for_profiling(self) -> ProfilingRunConfig:
        """The subset the profiling run reads, so the rest cannot be relied on."""
        return ProfilingRunConfig(
            cov_source=self.cov_source,
            cov_source_origin=self.cov_source_origin,
            pytest_args=self.pytest_args,
            allow_ordered=self.allow_ordered,
            iterations=self.iterations,
        )


@dataclass(frozen=True)
class DownwindConfig:
    """Fully resolved configuration for the downwind command.

    Separate from :class:`ResolvedConfig` rather than a mode of it: downwind
    has no time cap, no coverage target and no coverage source to discover,
    and giving it a ResolvedConfig would mean handing it a mode it does not
    have and a cov_source it never reads. The two share the one
    ``[tool.smoke_optimiser]`` table and the one precedence rule; only the
    resolved shapes differ.

    Attributes:
        profile_path: Where the profile both commands agree on lives. Shared
            with ResolvedConfig, since downwind reads exactly the file the
            profiling phase writes.
        downwind_file_path: Where the selection is written, and where the
            pytest plugin is told to read it from.
        pytest_args: Extra arguments for the selective run. Deliberately not
            ResolvedConfig.pytest_args, which carries the profiling run's
            coverage flags -- forwarding those would instrument every commit.
        regenerate_on_fallback: Whether a run that falls back to the full suite
            runs that suite instrumented and rewrites the profile it fell back
            from. On by default: the full suite is being paid for either way,
            and without it staleness ratchets -- the map expires, and every
            commit pays a full suite until someone regenerates it by hand.
        profiling: The settings that fallback run uses. Its ``pytest_args`` are
            the PROFILING ones, not this config's: the profile a fallback
            writes has to be the profile ``smoke --profile-only`` would have
            written, and a per-commit ``-x`` or ``-k`` in the downwind args
            would narrow collection into a profile of part of the suite that
            every completeness check would nonetheless pass.
        environment_files: Patterns naming the paths that define the
            environment the suite runs in, which force the full suite whatever
            the maps say. The shipped defaults plus whatever the project added,
            already combined -- the config key is spelled
            ``extra_environment_files`` precisely so that setting it cannot
            silently drop a default and with it the full-suite guard on a
            lockfile. Configurable at all, where a list of paths to IGNORE
            would not be, because every entry can only ADD a full-suite run: a
            wrong one costs time rather than correctness.
    """

    profile_path: Path
    downwind_file_path: Path
    pytest_args: str
    regenerate_on_fallback: bool
    profiling: ProfilingRunConfig
    environment_files: list[str] = field(default_factory=lambda: list(DEFAULT_ENVIRONMENT_FILES))


class ProjectMetadata(BaseModel):
    """Minimal project metadata from pyproject.toml."""

    name: str


class CoverageRunConfig(BaseModel):
    """The part of [tool.coverage.run] that says what coverage measures.

    Read because a project that has configured coverage has already answered the
    question ``--src`` asks, and guessing past an explicit answer is how a
    profile ends up instrumenting the whole repository nobody asked it to.
    """

    source: list[str] = Field(default_factory=list)
    source_pkgs: list[str] = Field(default_factory=list)


class CoverageConfig(BaseModel):
    """The [tool.coverage] section of pyproject.toml."""

    run: CoverageRunConfig | None = None


class ToolConfig(BaseModel):
    """Tool configuration section in pyproject.toml."""

    smoke_optimiser: FileConfig | None = None
    coverage: CoverageConfig | None = None


class PyProjectConfig(BaseModel):
    """Structure of pyproject.toml for validation."""

    project: ProjectMetadata | None = None
    tool: ToolConfig | None = None


def _read_pyproject(project_root: Path) -> PyProjectConfig | None:
    """Parse pyproject.toml, or say why it could not be read and carry on."""
    pyproject_path = project_root / "pyproject.toml"
    if not pyproject_path.exists():
        return None
    try:
        with pyproject_path.open("rb") as f:
            return PyProjectConfig.model_validate(tomllib.load(f))
    except (tomllib.TOMLDecodeError, OSError, ValidationError) as e:
        typer.secho(
            f"⚠️ Warning: Failed to parse pyproject.toml while looking for a coverage source: {e}",
            fg=typer.colors.YELLOW,
            err=True,
        )
        return None


def _cov_target_from_coverage_config(data: PyProjectConfig | None) -> DiscoveredCovSource | None:
    """What the project already told coverage.py to measure.

    Ahead of every guess below it, because it is not a guess: a project with a
    [tool.coverage.run] source has stated the answer, and reaching past it to a
    heuristic is exactly how a profile ends up wider than the project meant.
    """
    if data is None or data.tool is None or data.tool.coverage is None or data.tool.coverage.run is None:
        return None

    run = data.tool.coverage.run
    declared = run.source or run.source_pkgs
    if not declared:
        return None

    key = "source" if run.source else "source_pkgs"
    note = ""
    if len(declared) > 1:
        note = (
            f"[tool.coverage.run] {key} names {len(declared)} entries and only the first is used; "
            "set [tool.smoke_optimiser] cov_source to choose"
        )
    return DiscoveredCovSource(
        value=declared[0],
        origin=CovSourceOrigin.COVERAGE_CONFIG,
        note=note,
    )


def _cov_target_from_project_name(data: PyProjectConfig | None, project_root: Path) -> DiscoveredCovSource | None:
    """A package named after the project, which is the commonest flat layout."""
    if data is None or data.project is None or not data.project.name:
        return None
    normalised = data.project.name.replace("-", "_")
    if not (project_root / normalised).is_dir():
        return None
    return DiscoveredCovSource(value=normalised, origin=CovSourceOrigin.PROJECT_NAME)


def _discover_cov_target(project_root: Path) -> DiscoveredCovSource:
    """Work out what to instrument, and remember how that answer was reached.

    Explicit configuration first, guesses after, and no answer at all rather than
    quietly settling on the whole repository: instrumenting everything puts every
    .py file in the tree in scope, including the ones coverage.py never walks, and
    a profile that can never know them is a profile that has permanently expired.
    """
    data = _read_pyproject(project_root)

    from_coverage = _cov_target_from_coverage_config(data)
    if from_coverage is not None:
        return from_coverage

    if (project_root / "src").is_dir():
        return DiscoveredCovSource(value="src", origin=CovSourceOrigin.SRC_LAYOUT)

    from_name = _cov_target_from_project_name(data, project_root)
    if from_name is not None:
        return from_name

    return DiscoveredCovSource(value=WHOLE_REPOSITORY, origin=CovSourceOrigin.UNDISCOVERED)


def load_file_config(project_root: Path) -> FileConfig | None:
    """Load configuration from pyproject.toml in project root."""
    pyproject_path = project_root / "pyproject.toml"
    if not pyproject_path.exists():
        return None

    try:
        with pyproject_path.open("rb") as f:
            raw_data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return None

    data = PyProjectConfig.model_validate(raw_data)

    if data.tool is None or data.tool.smoke_optimiser is None:
        return None

    return data.tool.smoke_optimiser


def _apply_cli_overrides(file_config: FileConfig | None, cli_overrides: dict[str, Any]) -> FileConfig:
    """Lay the CLI's stated settings over the file's, defaults underneath.

    A value of None means "not passed", which is why every boolean flag is
    declared as a pair: a plain False would be indistinguishable from silence
    and would clobber the file on every run.

    Returns a FileConfig rather than the merged dict, so each resolver below
    reads named, typed attributes off it. Keys the model does not declare --
    the mode selectors, which are not settings -- are dropped here rather
    than in each resolver.
    """
    base = file_config or FileConfig()
    stated = {
        key: value for key, value in cli_overrides.items() if key in FileConfig.model_fields and value is not None
    }
    return base.model_copy(update=stated)


def resolve_downwind_config(
    file_config: FileConfig | None,
    cli_overrides: dict[str, Any],
    project_root: Path,
) -> DownwindConfig:
    """Merge defaults, file config and CLI overrides for the downwind command.

    Takes ``project_root`` for the same reason :func:`resolve_config` does: a
    fallback run has to instrument something, and a project that never set
    ``cov_source`` needs the same heuristic discovery the smoke path gets
    rather than a different answer on the downwind path.
    """
    resolved = _apply_cli_overrides(file_config, cli_overrides)
    discovered = (
        DiscoveredCovSource(value=resolved.cov_source, origin=CovSourceOrigin.CONFIGURED)
        if resolved.cov_source is not None
        else _discover_cov_target(project_root)
    )
    return DownwindConfig(
        profile_path=resolved.profile_path,
        downwind_file_path=resolved.downwind_file_path,
        pytest_args=resolved.downwind_pytest_args,
        regenerate_on_fallback=resolved.regenerate_on_fallback,
        profiling=ProfilingRunConfig(
            cov_source=discovered.value,
            cov_source_origin=discovered.origin,
            pytest_args=resolved.pytest_args,
            allow_ordered=resolved.allow_ordered,
            # One pass, whatever the project configured. Iterations exist to average
            # durations, which only the optimiser reads; the maps a fallback is
            # regenerating come out the same on every pass.
            iterations=1,
        ),
        environment_files=[*DEFAULT_ENVIRONMENT_FILES, *resolved.extra_environment_files],
    )


def resolve_config(
    file_config: FileConfig | None,
    cli_overrides: dict[str, Any],
    project_root: Path,
) -> ResolvedConfig:
    """Merge default config, file config, and CLI overrides into a final ResolvedConfig."""
    resolved = _apply_cli_overrides(file_config, cli_overrides)

    # Determine mode from CLI overrides first
    profile_only = cli_overrides.get("profile_only", False)
    optimise_only = cli_overrides.get("optimise_only", False)

    if profile_only:
        mode = OperationMode.PROFILE_ONLY
    elif optimise_only:
        mode = OperationMode.OPTIMISE_ONLY
    else:
        mode = OperationMode.FULL

    # If cov_source is still None (not in file and not in CLI), discover it -- and
    # keep how it was found, since a value nobody chose must not be silently profiled.
    discovered = (
        DiscoveredCovSource(value=resolved.cov_source, origin=CovSourceOrigin.CONFIGURED)
        if resolved.cov_source is not None
        else _discover_cov_target(project_root)
    )

    return ResolvedConfig(
        mode=mode,
        time_cap=resolved.time_cap,
        target_cov=resolved.target_cov,
        include_mandatory=resolved.include_mandatory,
        exclude_mandatory=resolved.exclude_mandatory,
        pytest_args=resolved.pytest_args,
        output_json=resolved.output_json,
        allow_ordered=resolved.allow_ordered,
        cov_source=discovered.value,
        cov_source_origin=discovered.origin,
        iterations=resolved.iterations,
        allow_parallel_durations=resolved.allow_parallel_durations,
        profile_path=resolved.profile_path,
    )
