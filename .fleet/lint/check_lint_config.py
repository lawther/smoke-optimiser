#!/usr/bin/env -S uv run --script
# Fleet-managed copy of fleet payload/lint/check_lint_config.py. Do not edit it here:
#      edit it there, then run `just sync` in fleet.
# /// script
# requires-python = ">=3.14"
# dependencies = [
#     "pydantic>=2.12",
# ]
# ///
"""Fail when this repo's own lint config does not pick up the fleet's shared ruff config whole, or lints .fleet/.

The fleet delivers `ruff_shared.toml` and `ty.toml` into the repo's lint config directory
(`.fleet/manifest.json` records where). `ty.toml` IS the config ty reads. `ruff_shared.toml` is
NOT read on its own: ruff auto-discovers `ruff.toml`, `.ruff.toml` and `pyproject.toml`, and
nothing else. Each repo carries a hand-written `ruff.toml` that says
`extend = "ruff_shared.toml"` and adds what that repo alone needs. That file belongs to the
repo and is never synced.

That makes the per-repo file a precondition, and every way of getting it wrong is silent. In
all four of these the delivered bytes still compare equal, so the integrity check stays green:

  - no `ruff.toml` at all, and `ruff_shared.toml` is never read by anything
  - a `ruff.toml` that does not extend it, same result
  - a `ruff.toml` that sets `select`, `ignore` or `per-file-ignores` rather than their
    `extend-` forms, which REPLACE the shared values rather than adding to them
  - a `[tool.ruff]` or `[tool.ty]` table left in pyproject.toml, which a ruff.toml or
    ty.toml overrides outright, so it reads as configuration and is not

It also refuses a pyproject.toml whose [tool.ml-lints] does not exclude `.fleet/`. fleet lints
the files it delivers there at the source, at the fleet's Python floor, and ruff and ty skip them
through the shared config. ml-lints parses every file with the repo's own interpreter, so in a
repo pinned below the floor it rejects delivered syntax that repo's Python cannot read.

This script judges the repo at the working directory, or at the repo root given as its only
argument. fleet loads it as well, and runs `check_config_dir` over a target before it
will deliver the shared config there.
"""

from __future__ import annotations

import argparse
import enum
import importlib
import pathlib
import sys
import tomllib
from typing import NamedTuple

# Importing a sibling would otherwise leave a __pycache__ beside the delivered copies, an
# untracked directory in .fleet/ that nothing owns.
sys.dont_write_bytecode = True
_CORE = pathlib.Path(__file__).resolve().parents[1] / "core"
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))
check_integrity = importlib.import_module("check_integrity")

LINT_FAMILY = "lint"
SHARED_RUFF = "ruff_shared.toml"
PYPROJECT = "pyproject.toml"

# Every name ruff auto-discovers a per-repo config under, in its own precedence order.
RUFF_CONFIG_NAMES = ("ruff.toml", ".ruff.toml")

# Keys whose plain form REPLACES what ruff_shared.toml sets, where the `extend-` form adds to
# it. A repo reaching for the plain form loses the shared value with nothing said, so these
# are refused rather than warned about. Measured on ruff 0.16.5.
CLOBBERING_LINT_KEYS = {
    "select": "extend-select",
    "ignore": "extend-ignore",
    "per-file-ignores": "extend-per-file-ignores",
    "fixable": "extend-fixable",
    "unfixable": "extend-unfixable",
}

# The pyproject tables a deployed ruff.toml or ty.toml silently overrides.
SUPERSEDED_TOOL_TABLES = ("ruff", "ty")

# fleet lints what it delivers into .fleet/ at the source, at the fleet's Python floor. ruff and ty
# skip .fleet/ through the shared config; ml-lints has no shared config, so each repo's pyproject
# must say so. Without it, a repo pinned below the floor parses delivered files it cannot read and
# fails its gate. Either key will do: `exclude` replaces ml-lints' defaults, `extend-exclude` adds.
ML_LINTS_TABLE = "ml-lints"
ML_LINTS_EXCLUDE_KEYS = ("extend-exclude", "exclude")
FLEET_DIR = ".fleet"


class Problem(enum.Enum):
    """Why one thing about the lint config fails."""

    UNREADABLE = enum.auto()  # a file this check must understand is not valid TOML
    NO_RUFF_CONFIG = enum.auto()  # nothing ruff discovers, so the shared file is inert
    NOT_EXTENDED = enum.auto()  # a ruff.toml that does not extend the shared file
    CLOBBERS_SHARED = enum.auto()  # a ruff.toml that replaces shared values instead of adding
    SUPERSEDED_TABLE = enum.auto()  # dead [tool.ruff]/[tool.ty] left in pyproject.toml
    NOT_RECORDED = enum.auto()  # the manifest does not say where the shared config was delivered
    FLEET_NOT_EXCLUDED = enum.auto()  # ml-lints would parse .fleet/ under this repo's interpreter


class Finding(NamedTuple):
    """One thing wrong with a repo's lint config: the file it is about, the cause, and what to do."""

    subject: pathlib.Path
    problem: Problem
    detail: str


def _load_toml(path: pathlib.Path) -> dict[str, object] | None:
    """`path` parsed, or None when it is not valid TOML."""
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError, UnicodeDecodeError:
        return None


def _table(data: dict[str, object], key: str) -> dict[str, object]:
    """`data[key]` when it is a table, an empty one otherwise.

    A malformed value here is not this check's business to diagnose: ruff will refuse it
    far more clearly than a message from here would. Treating it as empty means the checks
    below look at what is really there and say nothing about what is not.
    """
    value = data.get(key)
    return value if isinstance(value, dict) else {}


def ruff_config_path(config_dir: pathlib.Path) -> pathlib.Path | None:
    """The repo's own ruff config in `config_dir`, or None if it carries none."""
    for name in RUFF_CONFIG_NAMES:
        candidate = config_dir / name
        if candidate.exists():
            return candidate
    return None


def check_ruff_config(config_dir: pathlib.Path) -> list[Finding]:
    """What is wrong with this repo's own ruff config picking the shared file up, whole; empty when nothing."""
    found = ruff_config_path(config_dir)
    if found is None:
        names = " or ".join(RUFF_CONFIG_NAMES)
        return [
            Finding(
                config_dir,
                Problem.NO_RUFF_CONFIG,
                f"no {names} here, so nothing reads {SHARED_RUFF}: ruff discovers only those names "
                f'and pyproject.toml. Add one containing `extend = "{SHARED_RUFF}"`',
            )
        ]
    parsed = _load_toml(found)
    if parsed is None:
        return [Finding(found, Problem.UNREADABLE, "not valid TOML")]
    if parsed.get("extend") != SHARED_RUFF:
        return [
            Finding(
                found,
                Problem.NOT_EXTENDED,
                f'does not say `extend = "{SHARED_RUFF}"`, so the shared config is never read '
                f"even though its bytes are in sync (found extend = {parsed.get('extend')!r})",
            )
        ]
    clobbered = sorted(set(_table(parsed, "lint")) & set(CLOBBERING_LINT_KEYS))
    if clobbered:
        advice = ", ".join(f"{key} -> {CLOBBERING_LINT_KEYS[key]}" for key in clobbered)
        return [
            Finding(
                found,
                Problem.CLOBBERS_SHARED,
                f"[lint] sets {', '.join(clobbered)}, which REPLACES what {SHARED_RUFF} sets rather "
                f"than adding to it, so the shared rules vanish silently. Use the extend- form: {advice}",
            )
        ]
    return []


def check_superseded_tables(config_dir: pathlib.Path) -> list[Finding]:
    """The tables pyproject.toml still carries that the delivered config overrides."""
    pyproject = config_dir / PYPROJECT
    if not pyproject.exists():
        return []
    parsed = _load_toml(pyproject)
    if parsed is None:
        return [Finding(pyproject, Problem.UNREADABLE, "not valid TOML")]
    tool = _table(parsed, "tool")
    return [
        Finding(
            pyproject,
            Problem.SUPERSEDED_TABLE,
            f"still carries [tool.{name}]. A delivered {name}.toml overrides it outright, with no "
            f"merge and no warning, so it reads as configuration and is not. Delete the table",
        )
        for name in SUPERSEDED_TOOL_TABLES
        if name in tool
    ]


def _names_fleet_dir(entry: object) -> bool:
    return isinstance(entry, str) and entry.rstrip("/") == FLEET_DIR


def check_ml_lints_exclude(config_dir: pathlib.Path) -> list[Finding]:
    """A finding when ml-lints, configured from this pyproject.toml, would lint .fleet/; empty otherwise."""
    pyproject = config_dir / PYPROJECT
    if not pyproject.exists():
        return [
            Finding(
                pyproject,
                Problem.FLEET_NOT_EXCLUDED,
                f"not found, so ml-lints runs on its defaults and parses {FLEET_DIR}/. Add one with "
                f'[tool.{ML_LINTS_TABLE}] extend-exclude = ["{FLEET_DIR}"]',
            )
        ]
    parsed = _load_toml(pyproject)
    if parsed is None:
        return [Finding(pyproject, Problem.UNREADABLE, "not valid TOML")]
    table = _table(_table(parsed, "tool"), ML_LINTS_TABLE)
    for key in ML_LINTS_EXCLUDE_KEYS:
        entries = table.get(key)
        if isinstance(entries, list) and any(_names_fleet_dir(entry) for entry in entries):
            return []
    return [
        Finding(
            pyproject,
            Problem.FLEET_NOT_EXCLUDED,
            f"[tool.{ML_LINTS_TABLE}] does not exclude {FLEET_DIR}/. fleet lints those files at the "
            f"source, at the fleet's Python floor, so a repo pinned below it cannot parse them. Add "
            f'"{FLEET_DIR}" to extend-exclude (or to exclude, if this repo already sets it)',
        )
    ]


def check_config_dir(config_dir: pathlib.Path) -> list[Finding]:
    """Everything that must be true of `config_dir` before the shared config means anything there."""
    return [*check_ruff_config(config_dir), *check_superseded_tables(config_dir), *check_ml_lints_exclude(config_dir)]


def delivered_config_dir(root: pathlib.Path) -> pathlib.Path | None:
    """Where the manifest says the shared ruff config was delivered, or None if it records no such file."""
    record = check_integrity.load(root).families.get(LINT_FAMILY)
    if record is None:
        return None
    for file in record.files:
        if pathlib.Path(file.path).name == SHARED_RUFF:
            return root / pathlib.Path(file.path).parent
    return None


def judge(root: pathlib.Path) -> list[Finding]:
    """What is wrong with the lint config of the repo at `root`; empty when nothing."""
    manifest = root / check_integrity.MANIFEST_PATH
    try:
        config_dir = delivered_config_dir(root)
    except FileNotFoundError:
        return [Finding(manifest, Problem.NOT_RECORDED, "not found: this repo has not been synced by fleet")]
    except check_integrity.pydantic.ValidationError:
        return [Finding(manifest, Problem.NOT_RECORDED, "is not a manifest sync wrote")]
    if config_dir is None:
        return [Finding(manifest, Problem.NOT_RECORDED, f"does not record where {SHARED_RUFF} was delivered")]
    return check_config_dir(config_dir)


def run(root: pathlib.Path) -> int:
    """Judge the repo at `root`, print what is wrong with it, and return the exit status."""
    findings = judge(root)
    for finding in findings:
        print(f"FAIL  {finding.subject}: {finding.detail}")
    if findings:
        return 1
    print(f"ok    ruff config extends {SHARED_RUFF} and adds only extend- forms; ml-lints excludes {FLEET_DIR}/")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("root", nargs="?", type=pathlib.Path, default=pathlib.Path(), help="repo root (default: .)")
    return run(parser.parse_args().root)


if __name__ == "__main__":
    sys.exit(main())
