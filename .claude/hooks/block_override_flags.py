#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# dependencies = []
# ///
"""Decide whether a shell command exists only to override a refusal.

A tool refusing is information. ``bd --force``, ``git push --force`` and ``--no-verify`` all
exist to silence a refusal, and silencing one is the user's decision, not the agent's. So is
publishing: ``git push``, ``bd dolt push`` and the ``gh pr`` writes are denied outright, because
a publish cannot be taken back. This script is the decision half of the ``PreToolUse`` guard:
the wrapper hands it one command string as ``argv[1]``, and it prints the deny payload, or
nothing at all.

Matching is on shell **tokens**, produced by :mod:`_shell_tokenise`, not on the raw command
string. A flag inside a quoted argument — a commit message, a ``bd --append-notes`` body,
this file's own prose — is data, not a flag, and must not trip the guard.

Matching is also scoped to a single **command segment** — the tokens between one `;`/`&&`/
`||`/`|`/`&`/newline and the next. `git`, `push` and a force flag must all name the same
piece of work, not merely all appear somewhere in a longer compound command: a heredoc body
that happens to discuss pushing, sitting beside an unrelated `git add` and a `[ -f file ]`
test in the same command string, must not be read as a force push. Segment-scoping still
does not require adjacency *within* a segment — `git push origin main --force` is caught
however its tokens are ordered — it only stops tokens from unrelated segments colliding.

Publishing is matched more precisely than the force flags: `git stash push` is a local stash, not
a publish, so the git **subcommand** is found by skipping git's global options (`-C <dir>`,
`-c k=v`, ...) rather than by looking for the word `push` anywhere. A global option this file
does not recognise could be hiding a push, so it fails closed.

Short flags are matched the way the command's own parser reads them: a cluster such as `-nm`
is read left to right and stops at an option that takes a value, so `git commit -nm x` skips
the gate while `git commit -mn` (a message of "n") does not.

Standard library only, and no third-party imports: the hook runs in its own PEP 723 environment
(`dependencies = []`), not the project's, so no project package is importable. `_shell_tokenise`
is a sibling file in this same directory, always synced alongside this one (see hooks.toml), so
it is not a third-party dependency — but it is loaded by path (`_load_shell_tokenise()`) rather
than a plain `import _shell_tokenise`: this file is synced standalone into `.claude/hooks/` in
several repos, and some run a static type checker whose module-resolution roots do not include
that directory, so a literal import naming a sibling module reads as unresolved there even
though the runtime import (Python always puts a script's own directory on `sys.path`) works
fine. Loading by path has no import name for a static checker to flag, which avoids asking every
consuming repo to configure around this file.
"""

from __future__ import annotations

import importlib.util
import itertools
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from types import ModuleType


def _load_shell_tokenise() -> ModuleType:
    """The sibling `_shell_tokenise.py`, loaded by path -- see the module docstring.

    Typed as a plain `ModuleType` rather than the sibling's own type: naming that type
    would itself require an import statement, reintroducing the unresolved-import problem
    this whole function exists to avoid.
    """
    path = Path(__file__).resolve().parent / "_shell_tokenise.py"
    spec = importlib.util.spec_from_file_location("_shell_tokenise", path)
    if spec is None or spec.loader is None:
        message = f"cannot load the shell tokeniser from {path}"
        raise RuntimeError(message)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_shell_tokenise = _load_shell_tokenise()
command_segments = _shell_tokenise.command_segments
parse = _shell_tokenise.parse


def _load_hook_log() -> ModuleType:
    """The sibling `_hook_log.py`, loaded by path -- see `_load_shell_tokenise()` above."""
    path = Path(__file__).resolve().parent / "_hook_log.py"
    spec = importlib.util.spec_from_file_location("_hook_log", path)
    if spec is None or spec.loader is None:
        message = f"cannot load the hook logger from {path}"
        raise RuntimeError(message)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_hook_log = _load_hook_log()

_FORCE_FLAG = "--force"
_SHORT_FORCE_FLAG = "-f"
_LEASE_FLAG_PREFIX = "--force-with-lease"
_NO_VERIFY_FLAG = "--no-verify"
# git accepts any unambiguous prefix of a long option; "--no-veri" is the shortest that is
# not also a prefix of "--no-verbose".
_NO_VERIFY_MIN_PREFIX = "--no-veri"
_END_OF_OPTIONS = "--"

_GIT_COMMAND = "git"
_PUSH = "push"
_COMMIT = "commit"
_SUBTREE = "subtree"
_BD_COMMAND = "bd"
_BD_CLOSE = "close"
_DOLT = "dolt"
_GH_COMMAND = "gh"
_PR = "pr"
# The `gh pr` subcommands that write to GitHub. `view`, `list`, `diff`, `checks` and `status` read.
_GH_PR_WRITES = frozenset({"create", "edit", "merge", "close", "reopen", "ready", "comment", "review"})

# git's global options (before the subcommand) that consume the next token as their value.
_GIT_VALUE_GLOBAL_OPTIONS = frozenset(
    {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--super-prefix", "--config-env"}
)
# git's global options that take no value, or only an attached `=value`.
_GIT_FLAG_GLOBAL_OPTIONS = frozenset(
    {
        "-p",
        "--paginate",
        "-P",
        "--no-pager",
        "--bare",
        "--no-replace-objects",
        "--literal-pathspecs",
        "--glob-pathspecs",
        "--noglob-pathspecs",
        "--icase-pathspecs",
        "--no-optional-locks",
        "--no-advice",
        "--exec-path",
        "--html-path",
        "--man-path",
        "--info-path",
        "-h",
        "--help",
        "-v",
        "--version",
    }
)


class Rule(NamedTuple):
    """One refusal-override pattern: a short name, and why the command is denied."""

    name: str
    reason: str


BD_FORCE = Rule(
    name="bd --force",
    reason=(
        "Blocked: 'bd --force' overrides a dependency the tracker is enforcing. A blocked bd "
        "operation is the graph telling you the work is not finishable yet — the error text "
        "'use --force to override' describes a mechanism, it does not grant permission. Stop "
        "and ask the user whether the dependency is wrong."
    ),
)

FORCE_PUSH = Rule(
    name="force push",
    reason=("Blocked: force push. Rewriting published history is the user's call, never the agent's. Stop and ask."),
)

SKIPPED_VERIFICATION = Rule(
    name="--no-verify",
    reason=(
        "Blocked: '--no-verify' skips the precommit gate. A failing gate is a finding to "
        "report to the user, not an obstacle to route around. Stop and ask."
    ),
)

PUBLISHES = Rule(
    name="publish",
    reason=(
        "Blocked: this publishes ('git push', 'bd dolt push' or a 'gh pr' write). Publishing "
        "is the user's alone, every time, however routine the change looks. Finish the commit, "
        "tell the user what is waiting to go out, and ask them to run it themselves (they can "
        "type '! <command>' in the prompt)."
    ),
)

UNPARSEABLE = Rule(
    name="unparseable command",
    reason=(
        "Blocked: this command's quoting could not be fully parsed, so it cannot be checked "
        "for override flags — and a shell would most likely choke on it too, for the same "
        "reason. Fix the quoting (an apostrophe outside double quotes is the usual cause) and "
        "try again."
    ),
)


class ShortFlag(NamedTuple):
    """One short flag to look for in a command's arguments, and how its parser reads clusters."""

    letter: str
    # Letters that end a cluster because the rest of the cluster is their value (`-mn` is a message "n").
    value_letters: str
    # The subset of those whose value is the next token when nothing follows them in the cluster.
    separate_value_letters: str
    # Long options whose value is the next token when written without `=`.
    separate_value_long_options: frozenset[str]


COMMIT_NO_VERIFY = ShortFlag(
    letter="n",
    value_letters="mFCctuS",
    separate_value_letters="mFCct",
    separate_value_long_options=frozenset(
        {
            "--message",
            "--file",
            "--author",
            "--date",
            "--template",
            "--reuse-message",
            "--reedit-message",
            "--fixup",
            "--squash",
            "--cleanup",
            "--trailer",
            "--pathspec-from-file",
        }
    ),
)
PUSH_FORCE = ShortFlag(
    letter="f",
    value_letters="o",
    separate_value_letters="o",
    separate_value_long_options=frozenset({"--repo", "--receive-pack", "--exec", "--push-option"}),
)
BD_CLOSE_FORCE = ShortFlag(
    letter="f",
    value_letters="r",
    separate_value_letters="r",
    separate_value_long_options=frozenset({"--reason", "--reason-file", "--session", "--actor", "--db", "--directory"}),
)


class GitInvocation(NamedTuple):
    """One `git` in a segment: its subcommand and the arguments after it."""

    subcommand: str
    args: list[str]
    # False when a global option this file does not recognise came before the subcommand, so
    # the subcommand could not be found.
    understood: bool


def _is_command(token: str, name: str) -> bool:
    """Whether ``token`` runs ``name``, bare or by path (`/usr/bin/git`)."""
    return Path(token).name == name


def _git_invocations(tokens: list[str]) -> list[GitInvocation]:
    """Every `git` in ``tokens`` with its subcommand found past git's global options."""
    invocations = []
    for start, token in enumerate(tokens):
        if not _is_command(token, _GIT_COMMAND):
            continue
        index = start + 1
        understood = True
        while index < len(tokens) and tokens[index].startswith("-"):
            option = tokens[index]
            name = option.split("=", 1)[0]
            if name in _GIT_VALUE_GLOBAL_OPTIONS:
                index += 1 if "=" in option else 2
            elif name in _GIT_FLAG_GLOBAL_OPTIONS:
                index += 1
            elif option.startswith(("-C", "-c")):
                index += 1  # `-Cdir` / `-ckey=value`, the value attached
            else:
                understood = False
                break
        subcommand = tokens[index] if index < len(tokens) else ""
        invocations.append(GitInvocation(subcommand, tokens[index + 1 :], understood))
    return invocations


def _has_short_flag(args: list[str], flag: ShortFlag) -> bool:
    """Whether ``args`` carry ``flag``, alone or inside a cluster, read the way its parser reads them."""
    skip_next = False
    for arg in args:
        if skip_next:
            skip_next = False
        elif arg == _END_OF_OPTIONS:
            return False
        elif arg.startswith("--"):
            skip_next = "=" not in arg and arg in flag.separate_value_long_options
        elif arg.startswith("-") and len(arg) > 1:
            for position, letter in enumerate(arg[1:], start=1):
                if letter == flag.letter:
                    return True
                if letter in flag.value_letters:
                    skip_next = position == len(arg) - 1 and letter in flag.separate_value_letters
                    break
    return False


def _has_adjacent(tokens: list[str], first: str, second: str) -> bool:
    return any(a == first and b == second for a, b in itertools.pairwise(tokens))


def _publishes(tokens: list[str]) -> bool:
    """Whether the segment pushes, or writes to a pull request."""
    for git in _git_invocations(tokens):
        if not git.understood or git.subcommand == _PUSH:
            return True
        if git.subcommand == _SUBTREE and _PUSH in git.args:
            return True
    if any(_is_command(token, _BD_COMMAND) for token in tokens) and _has_adjacent(tokens, _DOLT, _PUSH):
        return True
    return any(_is_command(token, _GH_COMMAND) for token in tokens) and any(
        _has_adjacent(tokens, _PR, verb) for verb in _GH_PR_WRITES
    )


def _is_no_verify(token: str) -> bool:
    """Whether ``token`` is `--no-verify` or an abbreviation git would accept for it."""
    return token.startswith(_NO_VERIFY_MIN_PREFIX) and _NO_VERIFY_FLAG.startswith(token)


def _skips_commit_gate(tokens: list[str]) -> bool:
    return any(
        git.subcommand == _COMMIT and _has_short_flag(git.args, COMMIT_NO_VERIFY) for git in _git_invocations(tokens)
    )


def _force_pushes(tokens: list[str]) -> bool:
    return any(git.subcommand == _PUSH and _has_short_flag(git.args, PUSH_FORCE) for git in _git_invocations(tokens))


def _force_closes(tokens: list[str]) -> bool:
    for index, token in enumerate(tokens):
        if _is_command(token, _BD_COMMAND) and _BD_CLOSE in tokens[index + 1 :]:
            return _has_short_flag(tokens[tokens.index(_BD_CLOSE, index) + 1 :], BD_CLOSE_FORCE)
    return False


def violated_rule(segments: list[list[str]]) -> Rule | None:
    """The rule some segment of ``segments`` breaks, or ``None`` when none is an override.

    Each segment is one command in the chain — the tokens between one `;`/`&&`/`||`/`|`/`&`/
    newline and the next — checked on its own, so a force flag in one piece of a compound
    command cannot combine with a `git`/`push` pair from an unrelated piece.
    """
    for tokens in segments:
        present = set(tokens)
        forced = (
            _FORCE_FLAG in present
            or _SHORT_FORCE_FLAG in present
            or any(token.startswith(_LEASE_FLAG_PREFIX) for token in tokens)
        )

        if ("bd" in present and _FORCE_FLAG in present) or _force_closes(tokens):
            return BD_FORCE
        # Deliberately loose: any segment naming both 'git' and 'push' alongside a force
        # flag. Over-blocking costs one question; under-blocking costs rewritten history.
        if ("git" in present and "push" in present and forced) or _force_pushes(tokens):
            return FORCE_PUSH
        if any(_is_no_verify(token) for token in tokens) or _skips_commit_gate(tokens):
            return SKIPPED_VERIFICATION
        if _publishes(tokens):
            return PUBLISHES
    return None


_COMMAND_ARGV_INDEX = 1  # argv[1] is the command under test; argv[0] is the script path
_MIN_ARGC = _COMMAND_ARGV_INDEX + 1


def main(argv: list[str]) -> None:
    """Print the PreToolUse deny payload when ``argv[1]`` overrides a refusal."""
    if len(argv) < _MIN_ARGC:
        return
    result = parse(argv[_COMMAND_ARGV_INDEX])
    # Fail closed, unlike `_shell_tokenise.tokenise()`'s default: this guard is a boundary,
    # not a nudge, so a command this script cannot fully account for is denied rather than
    # checked on whatever partial tokens it managed to produce. What remains unparseable
    # after the shared tokeniser's line/quote/heredoc stitching is genuinely broken shell
    # syntax that a real shell would reject too, not ordinary prose slipping through.
    rule = UNPARSEABLE if result.truncated else violated_rule(command_segments(result.tokens))
    if rule is None:
        _hook_log.log_decision("block_override_flags.py", "PreToolUse", "Bash", "allow")
        return
    _hook_log.log_decision("block_override_flags.py", "PreToolUse", "Bash", "deny", rule.reason)
    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": rule.reason,
            }
        },
        sys.stdout,
    )


if __name__ == "__main__":
    main(sys.argv)
