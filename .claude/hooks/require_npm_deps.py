#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# dependencies = []
# ///
"""PreToolUse(Bash) guard: in an npm-family repo, run npm tooling only after `_npm-deps`.

The synced `_npm-deps` just recipe makes node_modules match package-lock.json, and every
recipe that runs npm tooling depends on it. A command typed straight into the shell does
not: `npx vitest` after a regenerated lockfile tests the packages that happen to be
installed, not the ones the lockfile pins, and passes or fails on the wrong code. Fixing a
Dependabot PR is exactly when that happens, and the false green it gives is what this
guards against.

SCOPE. The hook acts only where the justfile found by walking up from the payload's `cwd`
carries the synced NPM_DEPS block, so it is inert in every repo without an npm project and
needs no manifest to tell it which repos those are.

WHAT IS DENIED. A command segment that executes package code: `npx`; anything run from
`node_modules/.bin/`; `node` running a script; and `npm` with a subcommand that runs a
package script or binary (`run`, `test`, `start`, `exec` and their aliases). The npm
subcommand is found past `--prefix`, `-C`, `--workspace` and `-w` and their values, so
`npm --prefix app test` is seen as `test`. Managing packages or the lockfile (`npm install`,
`npm ci`, `npm view`, `npm ls`, `npm audit`) stays allowed: a change to the lockfile changes
`_npm-deps`'s stamp hash, so the next recipe reinstalls anyway.

THE WAY THROUGH. A segment is allowed when an earlier segment of the same `&&` chain is
`just ... _npm-deps`. The stamp check costs milliseconds when node_modules is current, so
`just _npm-deps && npx vitest run src/foo.test.ts` keeps a targeted run cheap. Any other
separator ends the chain -- after `;`, `||`, `|`, `&` or a newline the install may have
failed and the command still runs.

Like the other Bash guards this is a nudge, not a boundary: `bash -c "npx ..."`, a
subshell or a wrapper such as `xargs` evades it, an unparseable tail is dropped by the
shared tokeniser, and any exception allows the command.

Standard library only; the sibling modules are loaded by path -- see check_bead_model.py's
module docstring for why.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import ModuleType


def _load_sibling(name: str) -> ModuleType:
    path = Path(__file__).resolve().parent / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        message = f"cannot load {name} from {path}"
        raise RuntimeError(message)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_shell_tokenise = _load_sibling("_shell_tokenise")
_hook_log = _load_sibling("_hook_log")

_HOOK_NAME = "require_npm_deps.py"
_INSTALL_RECIPE = "_npm-deps"
_BLOCK_MARKER = "# BEGIN SHARED RECIPES (NPM_DEPS)"
_JUSTFILE_NAMES = ("justfile", "Justfile", ".justfile")
_CHAIN = "&&"

# npm subcommands that execute package code, with the aliases npm accepts for each.
_NPM_RUNNING_SUBCOMMANDS = frozenset(
    {"run", "run-script", "rum", "urn", "test", "tst", "t", "start", "stop", "restart", "exec", "x"}
)
# npm options whose value is the next token, so it is not mistaken for the subcommand.
_NPM_VALUE_OPTIONS = frozenset({"--prefix", "-C", "--workspace", "-w"})
# node options whose value is the next token, so it is not mistaken for a script path.
_NODE_VALUE_OPTIONS = frozenset({"-e", "--eval", "-p", "--print", "-r", "--require", "--import"})
_ENV_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def allow() -> None:
    _hook_log.log_decision(_HOOK_NAME, "PreToolUse", "Bash", "allow")
    sys.exit(0)


def deny(reason: str) -> None:
    _hook_log.log_decision(_HOOK_NAME, "PreToolUse", "Bash", "deny", reason)
    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        },
        sys.stdout,
    )
    sys.exit(0)


def carries_npm_deps(start: Path) -> bool:
    """Whether the nearest justfile at or above `start` holds the synced NPM_DEPS block."""
    for directory in (start, *start.parents):
        for name in _JUSTFILE_NAMES:
            justfile = directory / name
            if justfile.is_file():
                return _BLOCK_MARKER in justfile.read_text(encoding="utf-8")
    return False


def first_operand(arguments: list[str], value_options: frozenset[str]) -> str:
    """The first argument that is not an option or an option's value; empty if there is none."""
    skip_next = False
    for argument in arguments:
        if skip_next:
            skip_next = False
            continue
        if argument.startswith("-"):
            skip_next = argument in value_options
            continue
        return argument
    return ""


def runs_package_code(segment: list[str]) -> bool:
    """Whether `segment` executes code out of node_modules."""
    words = segment
    while words and _ENV_ASSIGNMENT.match(words[0]):
        words = words[1:]
    if not words:
        return False
    program, arguments = words[0], words[1:]
    if program == "npx" or "node_modules/.bin/" in program:
        return True
    if program == "npm":
        return first_operand(arguments, _NPM_VALUE_OPTIONS) in _NPM_RUNNING_SUBCOMMANDS
    if program == "node":
        return first_operand(arguments, _NODE_VALUE_OPTIONS) != ""
    return False


def installs(segment: list[str]) -> bool:
    """Whether `segment` is a `just` call that runs `_npm-deps`."""
    return bool(segment) and segment[0] == "just" and _INSTALL_RECIPE in segment[1:]


def unguarded_segment(tokens: list[str]) -> list[str]:
    """The first segment that runs package code outside an `_npm-deps` chain; empty if none."""
    segment: list[str] = []
    installed = False
    for token in [*tokens, ";"]:
        if token not in _shell_tokenise.COMMAND_SEPARATORS:
            segment.append(token)
            continue
        if not installed and runs_package_code(segment):
            return segment
        installed = (installed or installs(segment)) and token == _CHAIN
        segment = []
    return []


def denial(segment: list[str]) -> str:
    command = " ".join(segment)
    return (
        f"Blocked: `{command}` runs package code from node_modules, which may not match "
        "package-lock.json -- after a lockfile change it tests the old packages and gives a false "
        f"result. Run the repo's just recipe for this instead, or chain the install first: "
        f"`just {_INSTALL_RECIPE} && {command}` (it costs milliseconds when node_modules is current)."
    )


def main() -> None:
    # Standard-library-only script; malformed hook input raises, and the __main__ guard
    # below turns any exception into an allow.
    hook_input = json.load(sys.stdin)  # noqa: ML400
    if not carries_npm_deps(Path(hook_input.get("cwd") or Path.cwd())):
        allow()
    command = (hook_input.get("tool_input") or {}).get("command", "")
    segment = unguarded_segment(_shell_tokenise.tokenise(command))
    if segment:
        deny(denial(segment))
    allow()


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001 - a hook bug must fail open, not block every Bash call
        allow()
