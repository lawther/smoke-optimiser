#!/usr/bin/env -S uv run --script
# Fleet-managed copy of agent_rules payload/rules/check_beads_block.py. Do not edit it here:
#      edit it there, then run `just sync` in agent_rules.
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Fail if a generated Beads integration block has reappeared in this repo.

`bd init` writes a `<!-- BEGIN BEADS INTEGRATION -->` block into AGENTS.md and
CLAUDE.md, and its "Agent Context Profiles" section asserts a conservative git
policy -- do not commit without being asked -- that contradicts global.md's
Committing Code section. The blocks were removed from the fleet once (see ar-pq5).
Nothing stops them coming back: bd re-emits the block verbatim in every new repo
`bd init` touches, and no bd command rewrites or verifies it afterwards, so a repo
enrolled later arrives carrying the contradiction with nothing to report it.

This script only reports. Deleting a generated block is a decision about the repo's own file,
and an automatic deletion would quietly fight `bd init` on every run.

Markdown at the repo's root only. A document *about* the block -- vaultscribe's
docs/agent-integration-analysis.md quotes one in full -- is not a repo instructing
an agent, and is not what this looks for.

This script judges the repo at the working directory, or at the repo root given as its only
argument.
"""

from __future__ import annotations

import argparse
import pathlib
import sys
from typing import NamedTuple

MARKER = "<!-- BEGIN BEADS INTEGRATION"


class Finding(NamedTuple):
    """One file still carrying a generated Beads block."""

    path: pathlib.Path
    line: int


def blocks_in(repo_root: pathlib.Path) -> list[Finding]:
    """Every root-level markdown file in `repo_root` that still carries the block."""
    found = []
    for path in sorted(repo_root.glob("*.md")):
        for number, text in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if text.startswith(MARKER):
                found.append(Finding(path, number))
                break
    return found


def run(root: pathlib.Path) -> int:
    """Judge the repo at `root`, print what is wrong with it, and return the exit status."""
    findings = blocks_in(root)
    for finding in findings:
        print(
            f"FAIL  {finding.path}:{finding.line}: generated Beads block. Delete it from"
            " <!-- BEGIN BEADS INTEGRATION --> to <!-- END BEADS INTEGRATION --> inclusive:"
            " its Agent Context Profiles section contradicts global.md's Committing Code policy,"
            " and global.md's Task Tracking section carries the guidance it duplicates"
        )
    if findings:
        return 1
    print("ok    no generated Beads block")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("root", nargs="?", type=pathlib.Path, default=pathlib.Path(), help="repo root (default: .)")
    return run(parser.parse_args().root)


if __name__ == "__main__":
    sys.exit(main())
