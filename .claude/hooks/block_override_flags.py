#!/usr/bin/env python3
"""Decide whether a shell command exists only to override a refusal.

A tool refusing is information. ``bd --force``, ``git push --force`` and ``--no-verify`` all
exist to silence a refusal, and silencing one is the user's decision, not the agent's. This
script is the decision half of the ``PreToolUse`` guard: the wrapper hands it one command
string as ``argv[1]``, and it prints the deny payload, or nothing at all.

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

Standard library only, and no third-party imports: the hook runs under the system
interpreter before any project environment is guaranteed. `_shell_tokenise` is a sibling
file in this same directory, always synced alongside this one (see hooks.toml), so it is
not a third-party dependency — but it is loaded by path (`_load_shell_tokenise()`) rather
than a plain `import _shell_tokenise`: this file is synced standalone into `.claude/hooks/`
in several repos, and some run a static type checker whose module-resolution roots do not
include that directory, so a literal import naming a sibling module reads as unresolved
there even though the runtime import (Python always puts a script's own directory on
`sys.path`) works fine. Loading by path has no import name for a static checker to flag,
which avoids asking every consuming repo to configure around this file.
"""

from __future__ import annotations

import importlib.util
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

_FORCE_FLAG = "--force"
_SHORT_FORCE_FLAG = "-f"
_LEASE_FLAG_PREFIX = "--force-with-lease"
_NO_VERIFY_FLAG = "--no-verify"


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

UNPARSEABLE = Rule(
    name="unparseable command",
    reason=(
        "Blocked: this command's quoting could not be fully parsed, so it cannot be checked "
        "for override flags — and a shell would most likely choke on it too, for the same "
        "reason. Fix the quoting (an apostrophe outside double quotes is the usual cause) and "
        "try again."
    ),
)


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

        if "bd" in present and _FORCE_FLAG in present:
            return BD_FORCE
        # Deliberately loose: any segment naming both 'git' and 'push' alongside a force
        # flag. Over-blocking costs one question; under-blocking costs rewritten history.
        if "git" in present and "push" in present and forced:
            return FORCE_PUSH
        if _NO_VERIFY_FLAG in present:
            return SKIPPED_VERIFICATION
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
        return
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
