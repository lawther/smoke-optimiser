#!/usr/bin/env python3
"""PreToolUse(Bash|Write|Edit) guard: keep the em dash out of prose an agent writes.

Agents reach for the em dash (U+2014) far more than the humans in this fleet's git history
do. Scanning every enrolled repo for it turns up thousands of hits, almost all of them in
design docs and bd issue bodies -- exactly the two places an agent writes prose unsupervised.
This hook stops the count from growing rather than trying to shrink it: it is a PreToolUse
guard, so it only ever sees the text a tool call is about to write, never a file already on
disk. Existing em dashes are untouched; a cleanup pass is a separate, deliberate exercise.

WHAT IS SCANNED. Two entry points, matching check_diary_prose.py's shape:

  Bash: the issue-body flags of `bd create`/`new`/`q`/`update` (--title, --description/-d,
  --design, --notes, --append-notes, --acceptance, --context and the --body-file/
  --design-file forms, whose files are read), the content argument of `bd remember`, and a
  `git commit`/`git commit --amend` message (-m/--message, repeatable).

  Write and Edit: markdown content only (`content` for Write, `new_string` alone for Edit) --
  the same scope check_diary_prose.py uses, since design docs are where the backlog is
  worst. Code comments and data files are not scanned: a source file may legitimately need
  to quote the character (a test fixture, a string constant), and that is not the prose this
  hook is guarding.

Fenced code blocks are stripped from Write/Edit content before matching, so a sample
demonstrating the character is not itself a hit.

A hit denies the write. Standard library only, and no third-party imports: this hook runs
under the system interpreter before any project environment is guaranteed. `_shell_tokenise`
and `_hook_log` are sibling files in this same directory, always synced alongside this one
(see hooks.toml), and loaded by path rather than a plain import for the reason
check_diary_prose.py's docstring gives: this file is synced standalone into `.claude/hooks/`
in several repos, and a literal import naming a sibling module reads as unresolved to a
static checker whose roots do not include that directory.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from types import ModuleType


def _load_shell_tokenise() -> ModuleType:
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
tokenise = _shell_tokenise.tokenise


def _load_hook_log() -> ModuleType:
    path = Path(__file__).resolve().parent / "_hook_log.py"
    spec = importlib.util.spec_from_file_location("_hook_log", path)
    if spec is None or spec.loader is None:
        message = f"cannot load the hook logger from {path}"
        raise RuntimeError(message)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_hook_log = _load_hook_log()

_EM_DASH = "—"

_BD_BODY_COMMANDS = frozenset({"create", "new", "q", "update"})
_BODY_FLAGS = frozenset(
    {
        "--title",
        "--description",
        "-d",
        "--design",
        "--notes",
        "--append-notes",
        "--acceptance",
        "--context",
    }
)
_BODY_FILE_FLAGS = frozenset({"--body-file", "--design-file"})
_MESSAGE_FLAGS = frozenset({"-m", "--message"})

_MARKDOWN_SUFFIXES = frozenset({".md", ".markdown"})

_FENCED_BLOCK_RE = re.compile(r"^[ \t]*(`{3,}|~{3,}).*?(?:^[ \t]*\1[ \t]*$|\Z)", re.MULTILINE | re.DOTALL)

# Flags of `bd remember` that take no value, so the token after them is still positional.
_BD_STANDALONE_FLAGS = frozenset({"--json", "--silent", "--quiet", "--force", "--help", "-h"})


def allow(tool_name: str) -> None:
    _hook_log.log_decision("check_no_em_dash.py", "PreToolUse", tool_name, "allow")
    sys.exit(0)


class Denial(NamedTuple):
    reason: str
    context: str


def deny(found: Denial, tool_name: str) -> None:
    _hook_log.log_denial("check_no_em_dash.py", "PreToolUse", tool_name, found.reason, found.context)
    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": found.reason,
            }
        },
        sys.stdout,
    )
    sys.exit(0)


def strip_fenced_blocks(text: str) -> str:
    return _FENCED_BLOCK_RE.sub("", text)


def denial(text: str, source: str, *, strip_code: bool = False) -> Denial | None:
    scannable = strip_fenced_blocks(text) if strip_code else text
    position = scannable.find(_EM_DASH)
    if position == -1:
        return None
    reason = f"Blocked: {source} contains an em dash (—). Rewrite with a comma, colon, or period instead."
    return Denial(reason=reason, context=_hook_log.surrounding_sentence(scannable, position, position + 1))


def flag_values(tokens: list[str], flags: frozenset[str]) -> list[str]:
    """Values given to any of `flags`, accepting both `--flag value` and `--flag=value`."""
    values = []
    for index, token in enumerate(tokens):
        name, separator, inline_value = token.partition("=")
        if name not in flags:
            continue
        if separator:
            values.append(inline_value)
        elif index + 1 < len(tokens):
            values.append(tokens[index + 1])
    return values


def read_file_flag_values(call: list[str]) -> list[str]:
    """Contents of each readable `--body-file`/`--design-file` argument; `-` (stdin) is skipped."""
    contents = []
    for path_arg in flag_values(call, _BODY_FILE_FLAGS):
        if path_arg == "-":
            continue  # stdin: not inspectable from a PreToolUse hook
        try:
            contents.append(Path(path_arg).read_text(encoding="utf-8"))
        except OSError:
            continue  # unreadable, or relative to a cwd we don't know: fail open
    return contents


def positional_arguments(tokens: list[str]) -> list[str]:
    """Tokens that are neither a flag nor the value of one."""
    positionals = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token.startswith("-"):
            index += 1 if "=" in token or token in _BD_STANDALONE_FLAGS else 2
            continue
        positionals.append(token)
        index += 1
    return positionals


def bd_denial(call: list[str]) -> Denial | None:
    rest = call[1:]
    if not rest:
        return None
    subcommand = rest[0]
    if subcommand in _BD_BODY_COMMANDS:
        texts = flag_values(rest, _BODY_FLAGS) + read_file_flag_values(rest)
        source = f"`bd {subcommand}`"
    elif subcommand == "remember":
        texts = positional_arguments(rest[1:])
        source = "`bd remember`"
    else:
        return None
    for text in texts:
        found = denial(text, source)
        if found is not None:
            return found
    return None


def git_denial(call: list[str]) -> Denial | None:
    rest = call[1:]
    if len(rest) < 1 or rest[0] != "commit":
        return None
    for message in flag_values(rest[1:], _MESSAGE_FLAGS):
        found = denial(message, "a `git commit` message")
        if found is not None:
            return found
    return None


def bash_denial(hook_input: dict) -> Denial | None:
    command = (hook_input.get("tool_input") or {}).get("command", "")
    for call in command_segments(tokenise(command)):
        if not call:
            continue
        if call[0] == "bd":
            found = bd_denial(call)
        elif call[0] == "git":
            found = git_denial(call)
        else:
            found = None
        if found is not None:
            return found
    return None


def governed_markdown(file_path: str) -> bool:
    return Path(file_path).suffix.lower() in _MARKDOWN_SUFFIXES


def file_denial(tool_name: str, hook_input: dict) -> Denial | None:
    tool_input = hook_input.get("tool_input") or {}
    file_path = tool_input.get("file_path") or ""
    if not governed_markdown(file_path):
        return None
    # Edit: only the replacement is this write's prose; the rest of the file is not.
    text = tool_input.get("content") if tool_name == "Write" else tool_input.get("new_string")
    if not text:
        return None
    return denial(text, f"`{Path(file_path).name}`", strip_code=True)


def main() -> None:
    hook_input = json.load(sys.stdin)  # noqa: ML400
    tool_name = hook_input.get("tool_name", "")
    if tool_name == "Bash":
        found = bash_denial(hook_input)
    elif tool_name in ("Write", "Edit"):
        found = file_denial(tool_name, hook_input)
    else:
        found = None
    if found is not None:
        deny(found, tool_name)
        return
    allow(tool_name)


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001 - a hook bug must fail open, not block every write
        allow("unknown")
