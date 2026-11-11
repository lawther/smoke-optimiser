#!/usr/bin/env -S uv run --script
# Fleet-managed copy of fleet payload/github/check_actions_pinned.py. Do not edit it here:
#      edit it there, then run `just sync` in fleet.
# /// script
# requires-python = ">=3.14"
# dependencies = []
# ///
"""Fail if this repo's workflows use a GitHub Action not pinned to a commit SHA.

A tag or branch can be moved by whoever controls the action, so CI could run different code
tomorrow with nothing in this repo changing; a full SHA cannot. Each pinned reference also
names, in a trailing comment, the ref it tracks, which is how update-deps knows where to
move it. `actions_refs.py`, this script's sibling, defines both.

Run `just update-deps` in a failing repo: it pins every reference it finds.

This script judges the repo at the working directory, or at the repo root given as its only
argument.
"""

from __future__ import annotations

import argparse
import importlib
import pathlib
import subprocess
import sys
from typing import NamedTuple

# Importing a sibling would otherwise leave a __pycache__ beside the delivered copies, an
# untracked directory in .fleet/ that nothing owns.
sys.dont_write_bytecode = True
_HERE = pathlib.Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
actions_refs = importlib.import_module("actions_refs")


class Violation(NamedTuple):
    path: pathlib.PurePosixPath
    action: str
    ref: str


class GitError(Exception):
    """git could not list the repo's tracked files."""


def tracked_files(root: pathlib.Path) -> list[str]:
    """The paths git tracks in the repo at `root`."""
    completed = subprocess.run(["git", "-C", str(root), "ls-files"], capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        message = f"git cannot list the files in {root}: {completed.stderr.strip()}"
        raise GitError(message)
    return completed.stdout.splitlines()


def violations(root: pathlib.Path, tracked: list[str]) -> list[Violation]:
    """Every remote action reference in the tracked workflows that is not pinned."""
    found = []
    for path in actions_refs.workflow_files(tracked):
        text = (root / str(path)).read_text(encoding="utf-8")
        found += [Violation(path, use.action, use.ref) for use in actions_refs.uses(text) if not use.pinned]
    return found


def run(root: pathlib.Path) -> int:
    """Judge the repo at `root`, print what is wrong with it, and return the exit status."""
    try:
        found = violations(root, tracked_files(root))
    except GitError as error:
        print(f"FAIL  {root}: {error}")
        return 1
    for violation in found:
        print(f"FAIL  {violation.path}: {violation.action}@{violation.ref} is not pinned to a commit SHA")
    if found:
        print(f"      unpinned action references: {len(found)}; run `just update-deps` to pin them")
        return 1
    print("ok    every workflow pins its actions")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("root", nargs="?", type=pathlib.Path, default=pathlib.Path(), help="repo root (default: .)")
    return run(parser.parse_args().root)


if __name__ == "__main__":
    sys.exit(main())
