import tomllib
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

import typer
from pydantic import BaseModel, Field, ValidationError


class OperationMode(Enum):
    """Execution modes for smoke-optimiser."""

    FULL = "full"
    PROFILE_ONLY = "profile-only"
    OPTIMISE_ONLY = "optimise-only"


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
    iterations: int
    allow_parallel_durations: bool
    profile_path: Path


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
    """

    profile_path: Path
    downwind_file_path: Path
    pytest_args: str


class ProjectMetadata(BaseModel):
    """Minimal project metadata from pyproject.toml."""

    name: str


class ToolConfig(BaseModel):
    """Tool configuration section in pyproject.toml."""

    smoke_optimiser: FileConfig | None = None


class PyProjectConfig(BaseModel):
    """Structure of pyproject.toml for validation."""

    project: ProjectMetadata | None = None
    tool: ToolConfig | None = None


def _discover_cov_target(project_root: Path) -> str:
    """Best-effort discovery of the source directory for coverage."""
    # 1. src/ layout is a very strong signal
    if (project_root / "src").is_dir():
        return "src"

    # 2. Package matching project name in pyproject.toml
    pyproject_path = project_root / "pyproject.toml"
    if pyproject_path.exists():
        try:
            with pyproject_path.open("rb") as f:
                raw_data = tomllib.load(f)
                data = PyProjectConfig.model_validate(raw_data)
                if data.project and data.project.name:
                    normalised = data.project.name.replace("-", "_")
                    # If there's a folder matching the project name, instrument it
                    if (project_root / normalised).is_dir():
                        return normalised
        except (tomllib.TOMLDecodeError, OSError, ValidationError) as e:
            typer.secho(
                f"⚠️ Warning: Failed to parse project name from pyproject.toml: {e}",
                fg=typer.colors.YELLOW,
                err=True,
            )

    return "."


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
) -> DownwindConfig:
    """Merge defaults, file config and CLI overrides for the downwind command."""
    resolved = _apply_cli_overrides(file_config, cli_overrides)
    return DownwindConfig(
        profile_path=resolved.profile_path,
        downwind_file_path=resolved.downwind_file_path,
        pytest_args=resolved.downwind_pytest_args,
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

    # If cov_source is still None (not in file and not in CLI), discover it
    cov_source = resolved.cov_source
    if cov_source is None:
        cov_source = _discover_cov_target(project_root)

    return ResolvedConfig(
        mode=mode,
        time_cap=resolved.time_cap,
        target_cov=resolved.target_cov,
        include_mandatory=resolved.include_mandatory,
        exclude_mandatory=resolved.exclude_mandatory,
        pytest_args=resolved.pytest_args,
        output_json=resolved.output_json,
        allow_ordered=resolved.allow_ordered,
        cov_source=cov_source,
        iterations=resolved.iterations,
        allow_parallel_durations=resolved.allow_parallel_durations,
        profile_path=resolved.profile_path,
    )
