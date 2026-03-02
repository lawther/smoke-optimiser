import shlex

from smoke_optimiser.config import OperationMode, ResolvedConfig


def build_repro_command(config: ResolvedConfig) -> str:
    """Build a canonical CLI command that reproduces the current configuration."""
    parts = ["smoke-optimiser"]

    # Exception: profile/optimise only flags are only present if given
    if config.mode == OperationMode.PROFILE_ONLY:
        parts.append("--profile-only")
    elif config.mode == OperationMode.OPTIMISE_ONLY:
        parts.append("--optimise-only")

    # Every other arg MUST be present
    parts.append(f"--time-cap={config.time_cap}")
    parts.append(f"--target-cov={config.target_cov}")

    # For lists, we must show them even if empty
    if not config.include_mandatory:
        parts.append("--include=''")
    else:
        for item in config.include_mandatory:
            parts.append(f"--include={shlex.quote(item)}")

    if not config.exclude_mandatory:
        parts.append("--exclude=''")
    else:
        for item in config.exclude_mandatory:
            parts.append(f"--exclude={shlex.quote(item)}")

    # For strings and paths, use --arg=val format
    parts.append(f"--pytest-args={shlex.quote(config.pytest_args)}")
    parts.append(f"--output-json={shlex.quote(str(config.output_json))}")

    if config.allow_ordered:
        parts.append("--allow-ordered")
    else:
        parts.append("--no-allow-ordered")

    parts.append(f"--smoke-file-path={shlex.quote(str(config.smoke_file_path))}")

    src_val = config.cov_source if config.cov_source is not None else ""
    parts.append(f"--src={shlex.quote(src_val)}")

    return " ".join(parts)
