import shlex

from smoke_optimiser.config import ResolvedConfig


def build_repro_command(config: ResolvedConfig) -> str:
    """Build a canonical CLI command that reproduces the current configuration."""
    parts = ["smoke-optimiser"]

    if config.mode.value == "profile-only":
        parts.append("--profile-only")
    elif config.mode.value == "optimise-only":
        parts.append("--optimise-only")

    parts.extend(["--time-cap", str(config.time_cap)])
    parts.extend(["--target-cov", str(config.target_cov)])

    for item in config.include_mandatory:
        parts.extend(["--include", item])

    for item in config.exclude_mandatory:
        parts.extend(["--exclude", item])

    if config.pytest_args:
        parts.extend(["--pytest-args", config.pytest_args])

    parts.extend(["--output-json", str(config.output_json)])

    if config.allow_ordered:
        parts.append("--allow-ordered")

    parts.extend(["--smoke-file-path", str(config.smoke_file_path)])

    return " ".join(shlex.quote(p) for p in parts)
