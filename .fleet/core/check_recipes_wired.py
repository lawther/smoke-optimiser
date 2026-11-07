#!/usr/bin/env -S uv run --script
# Fleet-managed copy of agent_rules payload/core/check_recipes_wired.py. Do not edit it here:
#      edit it there, then run `just sync` in agent_rules.
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Fail if this repo's gate does not run the fleet's checks, or its CI cannot run them.

The fleet's checks run through `precommit-fleet`, the core recipe that depends on every
family's gate. A repo whose own `precommit` recipe does not run it holds managed copies and
checks that nothing ever runs, and nothing else reports that: the recipe block is
byte-identical everywhere, so the integrity check stays green. A `just precommit-fleet` line
in the body of `precommit` counts as running it, and so does a dependency on it. A mention in
a comment, in another recipe, or in a longer recipe name does not.

Every gate runs its checks with `uv run --script` and a family's lint recipes run `uvx`, so a
repo's `.github/workflows/ci.yml` must install uv, or CI exits 127 while every local run stays
green. A repo with no ci.yml has no CI to break.

This script judges the repo at the working directory, or at the repo root given as its only
argument.
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys
from typing import NamedTuple

PRECOMMIT = "precommit"
PRECOMMIT_FLEET = "precommit-fleet"
_PRECOMMIT_HEADER = re.compile(rf"^{PRECOMMIT}\s*:(?!=)(?P<dependencies>[^#]*)")
_FLEET_DEPENDENCY = re.compile(rf"(?<![\w-]){PRECOMMIT_FLEET}(?![\w-])")
_FLEET_CALL = re.compile(rf"\bjust\s+{PRECOMMIT_FLEET}(?![\w-])")
_INSTALLS_UV = re.compile(r"^\s*(?:-\s+)?uses:\s*astral-sh/setup-uv@", re.MULTILINE)
_CI_WORKFLOW = pathlib.Path(".github/workflows/ci.yml")
# The names `just` looks for, compared case-insensitively because the fleet does not case them alike.
_JUSTFILE_NAMES = frozenset({"justfile", ".justfile"})


class PrecommitVerdict(NamedTuple):
    """Whether one justfile's `precommit` recipe runs `precommit-fleet`."""

    defines: bool
    wired: bool

    @property
    def failed(self) -> bool:
        return not self.wired


def find_justfile(root: pathlib.Path) -> pathlib.Path | None:
    """The repo's justfile, under whichever case the repo spells it, or None if it has none."""
    return next((path for path in sorted(root.iterdir()) if path.name.lower() in _JUSTFILE_NAMES), None)


def judge_precommit(justfile: pathlib.Path) -> PrecommitVerdict:
    """Read `justfile` and report whether its `precommit` recipe depends on or calls `precommit-fleet`."""
    defines = False
    wired = False
    in_recipe = False
    for line in justfile.read_text(encoding="utf-8").splitlines():
        header = _PRECOMMIT_HEADER.match(line)
        if header:
            defines = True
            in_recipe = True
            wired = wired or _FLEET_DEPENDENCY.search(header.group("dependencies")) is not None
        elif line and not line[0].isspace():
            in_recipe = False
        elif in_recipe and not line.lstrip().startswith("#"):
            wired = wired or _FLEET_CALL.search(line) is not None
    return PrecommitVerdict(defines, wired)


def ci_has_uv(repo_root: pathlib.Path) -> bool:
    """True if the repo's CI workflow installs uv, or the repo has no CI workflow to break."""
    workflow = repo_root / _CI_WORKFLOW
    return not workflow.is_file() or _INSTALLS_UV.search(workflow.read_text(encoding="utf-8")) is not None


def judge(root: pathlib.Path) -> list[str]:
    """One line per fault in the repo at `root`, each as `<subject>: <problem>`; empty when the repo is wired."""
    justfile = find_justfile(root)
    if justfile is None:
        return [f"{root}: no justfile"]
    faults = []
    precommit = judge_precommit(justfile)
    if precommit.failed:
        problem = f"{PRECOMMIT} does not run {PRECOMMIT_FLEET}" if precommit.defines else f"defines no {PRECOMMIT}"
        faults.append(f"{justfile}: {problem}")
    if not ci_has_uv(root):
        faults.append(f"{root / _CI_WORKFLOW}: does not install uv, which every fleet gate runs")
    return faults


def run(root: pathlib.Path) -> int:
    """Judge the repo at `root`, print what is wrong with it, and return the exit status."""
    faults = judge(root)
    for fault in faults:
        print(f"FAIL  {fault}")
    if faults:
        return 1
    print(f"ok    {PRECOMMIT} runs {PRECOMMIT_FLEET}, and CI installs uv or does not exist")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("root", nargs="?", type=pathlib.Path, default=pathlib.Path(), help="repo root (default: .)")
    return run(parser.parse_args().root)


if __name__ == "__main__":
    sys.exit(main())
