from pathlib import Path

import pytest
from pydantic import ValidationError

from smoke_optimiser.config import (
    CovSourceOrigin,
    FileConfig,
    OperationMode,
    _discover_cov_target,
    load_file_config,
    resolve_config,
    resolve_downwind_config,
)
from smoke_optimiser.downwind.environment_files import DEFAULT_ENVIRONMENT_FILES

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


def test_a_boolean_left_off_the_command_line_does_not_clobber_the_file(tmp_path: Path) -> None:
    """A flag the user never typed arrives as None, so the file's value survives.

    This is the regression this tri-state exists for: while the flags defaulted to
    False, every run overrode a pyproject.toml `allow_ordered = true` back to False
    and the setting was silently inert.
    """
    file_config = FileConfig(allow_ordered=True, allow_parallel_durations=True)
    resolved = resolve_config(file_config, {"allow_ordered": None, "allow_parallel_durations": None}, tmp_path)
    assert resolved.allow_ordered is True
    assert resolved.allow_parallel_durations is True


def test_a_boolean_given_on_the_command_line_beats_the_file_in_both_directions(tmp_path: Path) -> None:
    """The negative form is the whole reason the flags are tri-state rather than `flag or None`."""
    resolved = resolve_config(FileConfig(allow_ordered=True), {"allow_ordered": False}, tmp_path)
    assert resolved.allow_ordered is False

    resolved = resolve_config(FileConfig(allow_ordered=False), {"allow_ordered": True}, tmp_path)
    assert resolved.allow_ordered is True


def test_a_project_extends_the_environment_carve_out_rather_than_replacing_it(tmp_path: Path) -> None:
    """Naming your own must not silently drop the guard on a lockfile.

    A replacing key would let 'extra_environment_files = ["deploy/*.tf"]' turn
    a changed uv.lock from a full-suite run into a selection -- exactly the
    silent under-selection every other rule here is built to prevent.
    """
    (tmp_path / "pyproject.toml").write_text('[tool.smoke_optimiser]\nextra_environment_files = ["deploy/*.tf"]\n')

    config = resolve_downwind_config(load_file_config(tmp_path), {}, tmp_path)

    assert "deploy/*.tf" in config.environment_files
    assert "uv.lock" in config.environment_files


def test_the_carve_out_defaults_to_the_shipped_list(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[tool.smoke_optimiser]\ntime_cap = 5.0\n")

    config = resolve_downwind_config(load_file_config(tmp_path), {}, tmp_path)

    assert config.environment_files == list(DEFAULT_ENVIRONMENT_FILES)


def test_the_projects_own_coverage_configuration_beats_every_guess(tmp_path: Path) -> None:
    """A project that has configured coverage has already answered the question.

    Reaching past that to a heuristic is exactly how a profile ends up wider than
    the project meant: enphase_curtailer declares source = ["app"] and has no
    src/ directory and no package named after it, so the guesses fell through to
    the whole repository -- which put four unmeasurable hook scripts into scope
    and expired the profile permanently.
    """
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "curtailer"\n\n[tool.coverage.run]\nsource = ["app"]\n',
    )
    (tmp_path / "app").mkdir()

    discovered = _discover_cov_target(tmp_path)

    assert discovered.value == "app"
    assert discovered.origin is CovSourceOrigin.COVERAGE_CONFIG


def test_a_coverage_configuration_naming_several_sources_says_only_the_first_is_used(tmp_path: Path) -> None:
    """--src carries one value, so the other entries have to be mentioned rather than dropped."""
    (tmp_path / "pyproject.toml").write_text('[tool.coverage.run]\nsource = ["app", "lib"]\n')

    discovered = _discover_cov_target(tmp_path)

    assert discovered.value == "app"
    assert "2 entries" in discovered.note
    assert "cov_source" in discovered.note


def test_source_pkgs_is_read_when_source_is_absent(tmp_path: Path) -> None:
    """coverage.py accepts either spelling, so reading only one would miss half the projects."""
    (tmp_path / "pyproject.toml").write_text('[tool.coverage.run]\nsource_pkgs = ["app"]\n')

    assert _discover_cov_target(tmp_path).origin is CovSourceOrigin.COVERAGE_CONFIG


def test_finding_nothing_is_reported_as_undiscovered_rather_than_as_the_whole_repository(tmp_path: Path) -> None:
    """The value is still '.', but nobody chose it, and that difference is the point.

    Instrumenting everything puts every .py file in the tree into the profile's
    scope, including ones coverage.py never walks -- so a run must be able to tell
    'the user asked for the whole repository' from 'we could not work it out'.
    """
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "nothing-matches"\n')

    discovered = _discover_cov_target(tmp_path)

    assert discovered.value == "."
    assert discovered.origin is CovSourceOrigin.UNDISCOVERED


def test_a_stated_cov_source_is_marked_as_configured_rather_than_discovered(tmp_path: Path) -> None:
    """What the user said must never be announced back at them as a guess."""
    (tmp_path / "pyproject.toml").write_text('[tool.smoke_optimiser]\ncov_source = "."\n')

    config = resolve_config(load_file_config(tmp_path), {}, tmp_path)

    assert config.cov_source == "."
    assert config.cov_source_origin is CovSourceOrigin.CONFIGURED
