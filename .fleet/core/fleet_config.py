#!/usr/bin/env -S uv run --script
# Fleet-managed copy of fleet payload/core/fleet_config.py. Do not edit it here:
#      edit it there, then run `just sync` in fleet.
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pydantic>=2.12",
# ]
# ///
"""Validate this repo's fleet.toml, the file where the repo says what the fleet's checks should tolerate.

`fleet.toml` sits at the repo root and belongs to the repo, not to fleet: sync never
writes it. Each family's checks read the section named for that family, so a repo records a
deliberate exception beside the code it excuses, and the check that honours the exception is
delivered into the repo with it. A repo with no fleet.toml takes every default.

The models below are the file's only definition. Family checks import this module from
`.fleet/core/`, which every repo carries, and fleet loads it by path for the
same reason: the reader and the validator cannot disagree about the file's shape.

Running the script validates the file at the working directory, or at the repo root given as
its only argument, and prints one `FAIL` line per problem.

```toml
schema = 1

[npm]
project_dir = "app"
deps_exempt = ["refresh-locks"]

[github]
dependabot_exempt = ["cargo"]

[smoke]
python_project_dir = "api"
```
"""

from __future__ import annotations

import argparse
import enum
import pathlib
import sys
import tomllib
from typing import Literal, NamedTuple

import pydantic

FLEET_TOML = pathlib.Path("fleet.toml")
SCHEMA = 1
REPO_ROOT_DIR = "."


class Ecosystem(enum.Enum):
    """A Dependabot package ecosystem the security-group check understands.

    The value is the `package-ecosystem` name in dependabot.yml and in `dependabot_exempt`.
    """

    UV = "uv"
    NPM = "npm"
    CARGO = "cargo"
    GITHUB_ACTIONS = "github-actions"


class _Section(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid", frozen=True)


class Npm(_Section):
    """The `[npm]` section, read by the npm family's checks and by its `_npm-deps` recipe.

    `project_dir` is the directory holding this repo's package.json and package-lock.json,
    relative to the repo root. The synced `_npm-deps` recipe installs there, and the repo's
    justfile defines `npm_project_dir` to the same value.

    `deps_exempt` names recipes that run npm, npx, node or node_modules tooling yet must not
    depend on `_npm-deps`, so `check_npm_deps_wired` does not report them: one that
    regenerates the lockfile, which `npm ci` would refuse while the lock and package.json
    disagree; one that only probes for a tool; one that installs into another worktree.
    Record the reason in a comment beside each name.
    """

    project_dir: str = REPO_ROOT_DIR
    deps_exempt: frozenset[str] = frozenset()


class Github(_Section):
    """The `[github]` section, read by the github family's checks.

    `dependabot_exempt` lists the Dependabot ecosystems this repo deliberately leaves without a
    security-updates group, so `check_dependabot_security` does not report them. Record the
    reason in a comment beside it.
    """

    dependabot_exempt: frozenset[Ecosystem] = frozenset()


class Smoke(_Section):
    """The `[smoke]` section, read by the smoke family's `downwind` recipe.

    `python_project_dir` is the directory holding this repo's pyproject.toml, relative to the
    repo root. The recipe changes into it before running, because smoke-optimiser takes its
    config, its artefact paths and pytest's working directory from the directory it is
    invoked in. The repo's justfile defines `python_project_dir` to the same value.
    """

    python_project_dir: str = REPO_ROOT_DIR


class FleetConfig(_Section):
    """The whole of fleet.toml: a schema version and one optional section per family."""

    schema_version: Literal[1] = pydantic.Field(alias="schema")
    npm: Npm = pydantic.Field(default_factory=Npm)
    github: Github = pydantic.Field(default_factory=Github)
    smoke: Smoke = pydantic.Field(default_factory=Smoke)

    model_config = pydantic.ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class FleetConfigError(Exception):
    """fleet.toml is not a file the fleet's checks can read, with one entry in `problems` per fault."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


def default() -> FleetConfig:
    """The configuration of a repo with no fleet.toml: every section at its defaults."""
    return FleetConfig.model_validate({"schema": SCHEMA})


def _describe(error: pydantic.ValidationError) -> list[str]:
    return [
        f"{'.'.join(str(part) for part in problem['loc']) or 'the file'}: {problem['msg']}"
        for problem in error.errors()
    ]


def load(root: pathlib.Path) -> FleetConfig:
    """The fleet.toml at `root`, validated; every default when the repo has none."""
    path = root / FLEET_TOML
    if not path.is_file():
        return default()
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as error:
        raise FleetConfigError([f"not valid TOML: {error}"]) from None
    try:
        return FleetConfig.model_validate(raw)
    except pydantic.ValidationError as error:
        raise FleetConfigError(_describe(error)) from None


class Verdict(NamedTuple):
    """What validating one repo's fleet.toml found."""

    present: bool
    problems: list[str]


def judge(root: pathlib.Path) -> Verdict:
    """Validate the fleet.toml at `root`."""
    present = (root / FLEET_TOML).is_file()
    try:
        load(root)
    except FleetConfigError as error:
        return Verdict(present, error.problems)
    return Verdict(present, [])


def run(root: pathlib.Path) -> int:
    """Validate the fleet.toml at `root`, print what is wrong with it, and return the exit status."""
    verdict = judge(root)
    for problem in verdict.problems:
        print(f"FAIL  {FLEET_TOML}: {problem}")
    if verdict.problems:
        return 1
    print(f"ok    {FLEET_TOML}: valid" if verdict.present else f"ok    no {FLEET_TOML}: every default applies")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate this repo's fleet.toml.")
    parser.add_argument("root", nargs="?", type=pathlib.Path, default=pathlib.Path(), help="repo root (default: .)")
    return run(parser.parse_args().root)


if __name__ == "__main__":
    sys.exit(main())
