import tomllib
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


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
    smoke_file_path: Path
    cov_source: str | None


def load_file_config(project_root: Path) -> FileConfig | None:
    """Load configuration from pyproject.toml in project root."""
    pyproject_path = project_root / "pyproject.toml"
    if not pyproject_path.exists():
        return None

    try:
        with open(pyproject_path, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return None

    tool_config = data.get("tool", {}).get("smoke_optimiser")
    if tool_config is None:
        return None

    return FileConfig(**tool_config)


def resolve_config(
    file_config: FileConfig | None,
    cli_overrides: dict[str, Any],
) -> ResolvedConfig:
    """Merge default config, file config, and CLI overrides into a final ResolvedConfig."""
    # Start with defaults from FileConfig
    base_config = file_config if file_config else FileConfig()

    # Apply CLI overrides
    config_dict = base_config.model_dump()

    # Determine mode from CLI overrides first
    profile_only = cli_overrides.get("profile_only", False)
    optimise_only = cli_overrides.get("optimise_only", False)

    if profile_only:
        mode = OperationMode.PROFILE_ONLY
    elif optimise_only:
        mode = OperationMode.OPTIMISE_ONLY
    else:
        mode = OperationMode.FULL

    # Only override if CLI value is not None
    for key, value in cli_overrides.items():
        if key in config_dict and value is not None:
            config_dict[key] = value

    return ResolvedConfig(
        mode=mode,
        time_cap=config_dict["time_cap"],
        target_cov=config_dict["target_cov"],
        include_mandatory=config_dict["include_mandatory"],
        exclude_mandatory=config_dict["exclude_mandatory"],
        pytest_args=config_dict["pytest_args"],
        output_json=Path(config_dict["output_json"]),
        allow_ordered=config_dict["allow_ordered"],
        smoke_file_path=Path(config_dict["smoke_file_path"]),
        cov_source=config_dict["cov_source"],
    )
