#!/usr/bin/env python3
"""PreToolUse(Bash) guard: reject unrecognised `--type` values on `bd dep add` / `bd link`.

bd's dependency-type enum (per `bd dep add --help`) is: blocks, tracks, related,
parent-child, discovered-from, until, caused-by, validates, relates-to, supersedes.
`bd dep add --type <anything else>` does not error -- it silently stores an
unrecognised edge type, which `bd ready` and `bd blocked` then don't treat as blocking
at all. The issue looks wired up (`bd show` lists the dependency) but is not actually
blocked, so `bd ready` surfaces it as available work it should have withheld.

The concrete way this bites: `blocked-by` reads like it should be a dependency type --
it is the mirror image of `blocks`, and `bd dep add` even has a `--blocked-by` *flag*
with that exact spelling. But `--blocked-by` is a flag name (an alias for
`--depends-on`, taking an issue ID), not a member of the `--type` enum. This is not
hypothetical: it happened twice in this project's own graph (casey-eoa, then
casey-tusl/casey-5c4/casey-yq7h), both times with the same silent-corruption
signature -- `bd ready` listing beads that should have been blocked. See `bd memories
blocked-by` for the reproduction notes. `blocked-by` is one wrong value among many this
hook rejects, not a special case -- any string outside the documented enum gets the
same treatment, with a sharper message for this specific, previously-seen mistake.

Two entry points are checked:

COMMAND LINE. `bd dep add ... --type <value>` / `-t <value>`, and `bd link ...
--type <value>` / `-t <value>`, matched case-insensitively with underscores
normalised to hyphens. `bd link --help` documents a narrower subset of the enum than
`bd dep add --help` does; the full `dep add` set is used for both commands so a type
`link` accepts but doesn't advertise is not flagged as a false positive.

BULK FILE. `bd dep add --file some.jsonl` (or `--file -` reading stdin) accepts
newline-delimited JSON with a `"type"` field per edge. A path argument is read and
scanned for a `"type"` value outside the enum; `--file -` (stdin) is not inspectable
from a PreToolUse hook and is deliberately let through unchecked.

Standard library only, and no project imports: the hook runs under the system
interpreter before any project environment is guaranteed.
"""

from __future__ import annotations

import json
import re
import shlex
import sys
from pathlib import Path

_TYPE_FLAGS = frozenset({"-t", "--type"})
_FILE_FLAGS = frozenset({"-f", "--file"})
_COMMAND_SEPARATORS = frozenset({"&&", "||", ";", "|"})

# Matches a JSONL "type" field value once underscores/hyphens/case are normalised away.
_BULK_TYPE_RE = re.compile(r'"type"\s*:\s*"([^"]+)"')

# Per `bd dep add --help`. `bd link --help` documents a narrower subset; this hook
# validates both commands against the fuller `dep add` enum (see module docstring).
_VALID_TYPES = frozenset(
    {
        "blocks",
        "tracks",
        "related",
        "parent-child",
        "discovered-from",
        "until",
        "caused-by",
        "validates",
        "relates-to",
        "supersedes",
    }
)

_HOW_TO_FIX = (
    "Use one of: " + ", ".join(sorted(_VALID_TYPES)) + ". If you specifically want issue A to record "
    "'depends on B', use the `--blocked-by`/`--depends-on` *flag* (not --type): "
    "`bd dep add A --blocked-by B`."
)

_BLOCKED_BY_WHY = (
    "'blocked-by' is not a valid `--type` value -- it is the name of a *flag* "
    "(`--blocked-by`, an alias for `--depends-on`), not a member of the dependency-type "
    "enum. bd accepts it anyway and silently stores a type `bd ready`/`bd blocked` don't "
    "recognise, so the issue looks wired up but isn't actually blocked. This has already "
    "happened twice in this project's graph (casey-eoa, then casey-tusl/casey-5c4/"
    "casey-yq7h) -- see `bd memories blocked-by`."
)


def allow() -> None:
    sys.exit(0)


def deny(reason: str) -> None:
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


def tokenise(command: str) -> list[str]:
    try:
        return shlex.split(command)
    except ValueError:
        return command.split()


def command_segments(tokens: list[str]) -> list[list[str]]:
    """`tokens` split into per-command segments at bare `&&`/`||`/`;`/`|` tokens."""
    segments: list[list[str]] = [[]]
    for token in tokens:
        if token in _COMMAND_SEPARATORS:
            segments.append([])
        else:
            segments[-1].append(token)
    return [segment for segment in segments if segment]


def bd_segments(tokens: list[str]) -> list[list[str]]:
    """Segments of `tokens` that are a `bd` invocation -- `bd` in command position."""
    return [segment for segment in command_segments(tokens) if segment[0] == "bd"]


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


def normalise_type(raw: str) -> str:
    return raw.strip().strip("'\"").lower().replace("_", "-")


def is_dep_or_link_call(call: list[str]) -> bool:
    """True if `call` is `bd dep [add] ...` or `bd link ...` (the commands taking --type)."""
    rest = call[1:]
    if not rest:
        return False
    # `bd dep <id> --blocks <id>` has no --type; `bd dep add ...` and `bd dep <id> --type ...`
    # (rarer, but --type is a global-looking flag on the `dep` root) both do, so every `dep`
    # call is in scope, same as every `link` call.
    return rest[0] in ("link", "dep")


def type_denial(raw_type: str, source: str) -> str | None:
    """Denial reason for one `--type`/JSONL `"type"` value found in `source`, or None if valid."""
    normalised = normalise_type(raw_type)
    if normalised in _VALID_TYPES:
        return None
    if normalised == "blocked-by":
        return f"Blocked: {source} uses `blocked-by`. {_BLOCKED_BY_WHY} {_HOW_TO_FIX}"
    return f"Blocked: {source} names `{raw_type}`, which is not a documented dependency type. {_HOW_TO_FIX}"


def bulk_file_denial(call: list[str]) -> str | None:
    """Denial reason if a `--file` JSONL argument contains an invalid edge type."""
    for path_arg in flag_values(call, _FILE_FLAGS):
        if path_arg == "-":
            continue  # stdin: not inspectable from a PreToolUse hook, let it through
        try:
            text = Path(path_arg).read_text(encoding="utf-8")
        except OSError:
            continue  # unreadable/relative-to-a-cwd-we-don't-know: fail open
        for match in _BULK_TYPE_RE.finditer(text):
            denial = type_denial(match.group(1), f"`--file {path_arg}`")
            if denial is not None:
                return denial
    return None


def call_denial(call: list[str]) -> str | None:
    if not is_dep_or_link_call(call):
        return None
    for raw_type in flag_values(call, _TYPE_FLAGS):
        denial = type_denial(raw_type, f"`{' '.join(call)}`")
        if denial is not None:
            return denial
    return bulk_file_denial(call)


def main() -> None:
    # Standard-library-only script (see module docstring); every field access below is via
    # .get() with a None-safe fallback, and the __main__ guard below fails open on any
    # exception, so malformed hook input degrades to "allow" rather than raising.
    hook_input = json.load(sys.stdin)  # noqa: ML400
    command = (hook_input.get("tool_input") or {}).get("command", "")
    tokens = tokenise(command)

    for call in bd_segments(tokens):
        denial = call_denial(call)
        if denial is not None:
            deny(denial)
            return
    allow()


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001 - a hook bug must fail open, not block every Bash call
        allow()
