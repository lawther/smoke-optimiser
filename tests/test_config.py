from pathlib import Path

import pytest
from pydantic import ValidationError

from smoke_optimiser.config import (
    FileConfig,
    OperationMode,
    load_file_config,
    resolve_config,
)

DEFAULT_TIME_CAP = 15.0
DEFAULT_TARGET_COV = 100.0
CUSTOM_TIME_CAP = 30.0
CUSTOM_TARGET_COV = 80.0
CLI_TIME_CAP = 45.0


def test_file_config_defaults() -> None:
    config = FileConfig()
    assert config.time_cap == DEFAULT_TIME_CAP
    assert config.target_cov == DEFAULT_TARGET_COV
    assert config.include_mandatory == []
    assert config.exclude_mandatory == []
    assert config.pytest_args == ""
    assert config.output_json == Path("./.smoke_suite.json")
    assert config.allow_ordered is False
    assert config.iterations == 1


def test_file_config_validation() -> None:
    with pytest.raises(ValidationError):
        FileConfig(time_cap=-1.0)
    with pytest.raises(ValidationError):
        FileConfig(target_cov=101.0)
    with pytest.raises(ValidationError):
        FileConfig(iterations=0)


def test_load_file_config_no_file(tmp_path: Path) -> None:
    assert load_file_config(tmp_path) is None


def test_load_file_config_no_section(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text("""[tool.other]
key = 'value'""")
    assert load_file_config(tmp_path) is None


def test_load_file_config_success(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(f"""
[tool.smoke_optimiser]
time_cap = {CUSTOM_TIME_CAP}
target_cov = {CUSTOM_TARGET_COV}
include_mandatory = ["@pytest.mark.smoke"]
""")
    config = load_file_config(tmp_path)
    assert config is not None
    assert config.time_cap == CUSTOM_TIME_CAP
    assert config.target_cov == CUSTOM_TARGET_COV
    assert config.include_mandatory == ["@pytest.mark.smoke"]


def test_resolve_config_defaults(tmp_path: Path) -> None:
    resolved = resolve_config(None, {}, tmp_path)
    assert resolved.mode == OperationMode.FULL
    assert resolved.time_cap == DEFAULT_TIME_CAP
    assert resolved.iterations == 1


def test_resolve_config_modes(tmp_path: Path) -> None:
    assert resolve_config(None, {"profile_only": True}, tmp_path).mode == OperationMode.PROFILE_ONLY
    assert resolve_config(None, {"optimise_only": True}, tmp_path).mode == OperationMode.OPTIMISE_ONLY
    # profile_only wins if both are set
    assert (
        resolve_config(None, {"profile_only": True, "optimise_only": True}, tmp_path).mode == OperationMode.PROFILE_ONLY
    )


def test_resolve_config_merge(tmp_path: Path) -> None:
    file_config = FileConfig(time_cap=CUSTOM_TIME_CAP, target_cov=CUSTOM_TARGET_COV)
    # CLI overrides file
    resolved = resolve_config(file_config, {"time_cap": CLI_TIME_CAP}, tmp_path)
    assert resolved.time_cap == CLI_TIME_CAP
    assert resolved.target_cov == CUSTOM_TARGET_COV

    # CLI None does not override file
    resolved = resolve_config(file_config, {"time_cap": None}, tmp_path)
    assert resolved.time_cap == CUSTOM_TIME_CAP
