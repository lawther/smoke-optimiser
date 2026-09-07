"""Collect the changed-file set from git, expressed the way the profile expresses paths.

Downwind selection needs the union of staged, unstaged and untracked changes,
not the index alone: pytest runs against the worktree, so a selection made
from ``git diff --cached`` alone would test something other than what is
about to be committed. ``git status --porcelain=v2`` gives that union in one
call, plus rename detection, so this module shells out to it rather than
composing several diffs by hand.

Every path git reports is relative to whatever directory git ran in, so this
module is always invoked with ``cwd`` set to the repository root -- the
caller's responsibility, since repo-root discovery belongs in one place
rather than being reimplemented wherever a path needs to agree with the
profile's.
"""

from __future__ import annotations

import shutil
import subprocess
from enum import Enum
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from pathlib import Path


class GitStatusError(RuntimeError):
    """``git status`` could not be run, or git itself is unavailable.

    Raised rather than answered with an empty set: outside a git repository
    there is no diff to select from, and a caller that can gate on this
    exception refuses loudly instead of a selection that silently claims
    nothing changed.
    """


class ChangeKind(Enum):
    """How a changed path differs from ``HEAD``.

    The downwind rules treat a deletion, a rename and an addition
    differently, so the kind travels with the path rather than being
    re-derived later from the filesystem -- by the time the rules run, a
    deleted file no longer exists to ask.
    """

    ADDED = "added"
    MODIFIED = "modified"
    DELETED = "deleted"
    RENAMED = "renamed"


class ChangedFile(NamedTuple):
    """One repo-relative path and how it changed."""

    path: str
    kind: ChangeKind


def _require_git() -> str:
    git_path = shutil.which("git")
    if git_path is None:
        msg = "git not found in PATH"
        raise GitStatusError(msg)
    return git_path


def _run_git_status(repo_root: Path) -> str:
    git_path = _require_git()
    try:
        result = subprocess.run(  # noqa: S603
            [git_path, "status", "--porcelain=v2", "-z", "--untracked-files=all", "--find-renames"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        msg = f"failed to run git status in {repo_root}"
        raise GitStatusError(msg) from exc
    if result.returncode != 0:
        msg = f"git status in {repo_root} failed: {result.stderr.strip()}"
        raise GitStatusError(msg)
    return result.stdout


def _kind_from_xy(index_status: str, worktree_status: str) -> ChangeKind:
    """Combine the index-vs-HEAD and worktree-vs-index statuses into one kind.

    A deletion in either half means the worktree lacks the file -- the only
    fact that matters to pytest -- so a deletion anywhere wins over every
    other status. Otherwise a staged addition is an addition even if the
    worktree has since modified it further, since the file still did not
    exist at ``HEAD``. Everything else, including type changes and unmerged
    conflict markers, is a modification: the path changed and pytest runs
    against whatever the worktree now holds for it.
    """
    if worktree_status == "D" or index_status == "D":
        return ChangeKind.DELETED
    if index_status == "A":
        return ChangeKind.ADDED
    return ChangeKind.MODIFIED


def _parse_ordinary(fields: list[str]) -> ChangedFile:
    xy = fields[1]
    path = fields[8]
    return ChangedFile(path=path, kind=_kind_from_xy(xy[0], xy[1]))


def _parse_unmerged(fields: list[str]) -> ChangedFile:
    path = fields[10]
    return ChangedFile(path=path, kind=ChangeKind.MODIFIED)


def _parse_rename(orig_path: str) -> ChangedFile:
    """A type-2 record: a rename, resolved to its old path.

    Type-2 records also cover copies, but git status only emits those with
    ``--find-copies``, which this module does not pass -- so-7hr scopes
    renames only, so every type-2 record reaching here is a rename.
    """
    return ChangedFile(path=orig_path, kind=ChangeKind.RENAMED)


def _parse_status_v2(output: str) -> frozenset[ChangedFile]:
    tokens = output.split("\0")
    changed: set[ChangedFile] = set()
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token == "":
            i += 1
            continue
        record_type = token[0]
        if record_type == "1":
            changed.add(_parse_ordinary(token.split(" ", 8)))
            i += 1
        elif record_type == "2":
            orig_path = tokens[i + 1]
            changed.add(_parse_rename(orig_path))
            i += 2
        elif record_type == "u":
            changed.add(_parse_unmerged(token.split(" ", 10)))
            i += 1
        elif record_type == "?":
            changed.add(ChangedFile(path=token.split(" ", 1)[1], kind=ChangeKind.ADDED))
            i += 1
        else:
            i += 1
    return frozenset(changed)


def changed_files(repo_root: Path) -> frozenset[ChangedFile]:
    """The union of staged, unstaged and untracked changes below ``repo_root``.

    ``repo_root`` must be the repository's top level, already resolved by
    the caller -- see the module docstring for why. Raises
    :class:`GitStatusError` if git is unavailable or ``repo_root`` is not
    inside a git repository, rather than returning an empty set that would
    read as "nothing changed".
    """
    return _parse_status_v2(_run_git_status(repo_root))
