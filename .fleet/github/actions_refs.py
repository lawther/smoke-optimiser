# Fleet-managed copy of fleet payload/github/actions_refs.py. Do not edit it here:
#      edit it there, then run `just sync` in fleet.
"""The `uses:` references in a GitHub Actions workflow, and whether each is pinned.

A pinned reference names a full 40-character commit SHA and carries, as a trailing comment,
the ref it tracks: `actions/checkout@<sha> # v7.0.1`, `dtolnay/rust-toolchain@<sha> # stable`.
The SHA makes what CI runs immutable; the comment is what update-deps follows to move it.
Local actions (`./...`) and `docker://` images are not GitHub actions and are never pinned here.

A library for `check_actions_pinned.py`, its sibling in `.fleet/github/`. fleet's update-deps
loads it by path to pin and move the references, so there is one definition of what a pinned
reference is. Standard library only.
"""

from __future__ import annotations

import pathlib
import re
from collections.abc import Iterable
from typing import NamedTuple

WORKFLOWS_DIR = pathlib.PurePosixPath(".github/workflows")
_WORKFLOW_SUFFIXES = frozenset({".yml", ".yaml"})
_USES = re.compile(
    r"(?m)^(?P<lead>[ \t]*(?:-[ \t]+)?uses:[ \t]*)(?P<quote>[\"']?)(?P<action>[^@\s\"'#]+)@(?P<ref>[^\s\"'#]+)"
    r"(?P=quote)(?P<comment>[ \t]+#[ \t]*(?P<note>[^\s#]+)[^\n]*)?"
)
_SHA = re.compile(r"^[0-9a-f]{40}$")


class Use(NamedTuple):
    action: str  # owner/repo, or owner/repo/path for an action in a subdirectory
    ref: str
    note: str  # the trailing comment's first word, "" when there is none

    @property
    def remote(self) -> bool:
        return not self.action.startswith(("./", "docker://"))

    @property
    def repository(self) -> str:
        return "/".join(self.action.split("/")[:2])

    @property
    def pinned(self) -> bool:
        return bool(_SHA.match(self.ref)) and bool(self.note)

    @property
    def tracked(self) -> str:
        """The ref this reference follows: its comment when pinned, else the ref itself."""
        return self.note if self.pinned else self.ref


def workflow_files(tracked: Iterable[str]) -> list[pathlib.PurePosixPath]:
    return [
        path
        for path in map(pathlib.PurePosixPath, sorted(tracked))
        if path.parent == WORKFLOWS_DIR and path.suffix in _WORKFLOW_SUFFIXES
    ]


def uses(text: str) -> list[Use]:
    """Every remote action reference in a workflow, in order."""
    found = [Use(match["action"], match["ref"], match["note"] or "") for match in _USES.finditer(text)]
    return [use for use in found if use.remote]


def repin(text: str, action: str, tracked: str, sha: str, note: str) -> str:
    """`text` with every reference to `action` that tracks `tracked` pinned to `sha # note`."""

    def replace(match: re.Match[str]) -> str:
        use = Use(match["action"], match["ref"], match["note"] or "")
        if use.action != action or use.tracked != tracked:
            return match[0]
        return f"{match['lead']}{action}@{sha} # {note}"

    return _USES.sub(replace, text)
