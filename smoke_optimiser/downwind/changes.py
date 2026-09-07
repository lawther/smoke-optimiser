"""Collect the changed-file set from git, expressed the way the profile expresses paths.

Downwind selection needs the union of staged, unstaged and untracked changes,
not the index alone: pytest runs against the worktree, so a selection made
from ``git diff --cached`` alone would test something other than what is
about to be committed. ``git status --porcelain=v2`` gives that union in one
call, plus rename detection, so this module shells out to it rather than
composing several diffs by hand.

Every path git reports is relative to whatever directory git ran in, so each
query here is given the repository root, which :func:`repository_root`
supplies. That discovery lives here rather than wherever a path happens to
need it, so there is one answer to "what are these paths relative to" rather
than one per caller.

:func:`tracked_files` sits alongside for the same reason: it is the other
half of what downwind selection asks of git -- the tree rather than the diff
-- and its paths must be spelled exactly as the diff's are.
"""

from __future__ import annotations

import shutil
import subprocess
from enum import Enum
from pathlib import Path
from typing import NamedTuple


class GitStatusError(RuntimeError):
    """A git query could not be run, or git itself is unavailable.

    Raised rather than answered with an empty set: outside a git repository
    there is no diff to select from, and a caller that can gate on this
    exception refuses loudly instead of a selection that silently claims
    nothing changed.

    The three parts of the diagnostic travel separately rather than as one
    prose string, because the caller renders them as a labelled block --
    what was tried, where, and what git itself said. Reconstructing that
    from a formatted message would mean parsing our own error text.

    Attributes:
        command: The git command that failed, as it would be typed.
        directory: The directory it ran in.
        detail: git's own stderr, or the reason it could not be started.
    """

    def __init__(self, *, command: str, directory: Path, detail: str) -> None:
        super().__init__(f"{command} in {directory} failed: {detail}")
        self.command = command
        self.directory = directory
        self.detail = detail


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


def _run_git(arguments: list[str], directory: Path) -> str:
    """Run one git query and return its stdout, or raise with what to report.

    Every failure mode ends in the same exception carrying the same three
    parts, so a caller renders one diagnostic rather than one per query.
    """
    printable = " ".join(["git", *arguments])
    git_path = shutil.which("git")
    if git_path is None:
        raise GitStatusError(command=printable, directory=directory, detail="git was not found on PATH")

    try:
        result = subprocess.run(  # noqa: S603
            [git_path, *arguments],
            cwd=directory,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise GitStatusError(command=printable, directory=directory, detail=str(exc)) from exc
    if result.returncode != 0:
        raise GitStatusError(command=printable, directory=directory, detail=result.stderr.strip())
    return result.stdout


def repository_root(directory: Path) -> Path:
    """The top level of the repository containing ``directory``.

    Repo-root discovery lives beside the diff that has to agree with it, so
    the two cannot drift: every path this module reports is relative to what
    this function returns.
    """
    top_level = _run_git(["rev-parse", "--show-toplevel"], directory).strip()
    return Path(top_level)


def tracked_files(repo_root: Path) -> frozenset[str]:
    """Every file git tracks, as repository-relative posix paths.

    Tracked only -- no ``--others`` -- because an untracked file is one just
    created, which :func:`changed_files` already reports, where the rules
    answer it with a more accurate reason than "the profile has never seen
    this". Excluding untracked files is also what lets .gitignore do the
    filtering, so a scope root of the whole repository does not need
    hand-rolled rules for .venv and its friends.

    A file staged for deletion is still an index entry and so still appears
    here, which over-selects in the one case it can matter -- a file created
    and deleted since the profile was captured -- and over-selecting is the
    safe direction.
    """
    output = _run_git(["ls-files", "-z"], repo_root)
    return frozenset(path for path in output.split("\0") if path)


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
    return _parse_status_v2(
        _run_git(["status", "--porcelain=v2", "-z", "--untracked-files=all", "--find-renames"], repo_root)
    )
