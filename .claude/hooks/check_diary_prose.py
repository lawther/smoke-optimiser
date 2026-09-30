#!/usr/bin/env python3
"""PreToolUse(Bash|Write|Edit) guard: keep diary prose out of durable artefacts.

global.md's *What You Write Describes Now, Not the Journey* says every durable artefact --
bd issues, design docs, READMEs, `bd remember` memories -- describes the work as it now
stands, and that history belongs in the commit message, a bd comment or a close reason.
Stated as prose alone the rule is followed for issues, which it names, and missed
everywhere else: a design doc narrating what is built versus not built, a paragraph citing
the commit that caused a change, "the defect was...", "survives as a diagnostic". This hook
puts the same rule where the writing happens.

WHAT IS SCANNED. Two entry points, and only the text going in -- never the artefact already
on disk, so an edit to a document that already contains diary prose is not blocked for
prose the current edit did not write.

  Bash: the issue-body flags of `bd create`/`new`/`q`/`update` (--title, --description/-d,
  --design, --notes, --append-notes, --acceptance, --context and the --body-file/--design-file
  forms, whose files are read), plus the content argument of `bd remember`.

  Write and Edit: markdown content -- `content` for Write, `new_string` alone for Edit.

WHAT IS NOT SCANNED, because the rule places history there deliberately: commit messages,
`bd close --reason`, `bd comment`, CHANGELOG.md, and any "Alternatives considered" or
"Rejected alternatives" section, which the rule keeps as its one exception. The rules files
themselves are skipped too -- they must quote the banned phrases in order to ban them.
Fenced code blocks are stripped before matching, so a phrase inside a sample is not a hit.

EVERY PATTERN IS A PHRASE, not a bare word. "now" on its own is ordinary present-tense
prose and is not matched; "now correctly" is a comparison with a past the reader cannot
see. The cost asymmetry is check_bead_model.py's: a false block is worse than a missed one,
so anything ambiguous is left out and a malformed payload allows.

A hit denies the write, which surfaces the reason to the agent. The reason names the
matched phrase and restates the test, so the response is to rewrite the sentence -- or, if
it genuinely describes present behaviour, to stop and ask rather than reword around the
pattern.

Standard library only, and no third-party imports: the hook runs under the system
interpreter before any project environment is guaranteed. `_shell_tokenise` is a sibling
file in this same directory, always synced alongside this one (see hooks.toml), so it is
not a third-party dependency -- but it is loaded by path (`_load_shell_tokenise()`) rather
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
import re
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
tokenise = _shell_tokenise.tokenise


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

_BD_BODY_COMMANDS = frozenset({"create", "new", "q", "update"})

# Issue fields the rule governs. `--reason` is absent on purpose: a close reason is one of
# the three places history is meant to live.
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

_MARKDOWN_SUFFIXES = frozenset({".md", ".markdown"})

# A changelog is a diary by design. The rules files quote the banned phrases to ban them.
_EXEMPT_FILENAMES = frozenset({"changelog.md", "global.md", "gemini.md", "claude.md", "agents.md"})

_FENCED_BLOCK_RE = re.compile(r"^[ \t]*(`{3,}|~{3,}).*?(?:^[ \t]*\1[ \t]*$|\Z)", re.MULTILINE | re.DOTALL)

# The sanctioned exception: an explicit rejected-alternative note, up to the next heading
# at the same or a higher level.
_ALTERNATIVES_RE = re.compile(
    r"^(?P<hashes>#{1,6})[ \t]*(?:alternatives[ \t]+considered|rejected[ \t]+alternatives)\b.*?"
    r"(?=^#{1,6}[ \t]|\Z)",
    re.MULTILINE | re.DOTALL | re.IGNORECASE,
)

# A 7-40 character hex string carrying at least one letter -- a sha, not a plain number.
_SHA = r"(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}"


class Pattern(NamedTuple):
    name: str
    regex: re.Pattern[str]


def _phrase(name: str, pattern: str, flags: int = re.IGNORECASE) -> Pattern:
    return Pattern(name=name, regex=re.compile(pattern, flags))


_PATTERNS = (
    _phrase(
        "an 'Update:' paragraph",
        r"^[ \t]*(?:[-*][ \t]*)?(?:\*\*)?update(?:\*\*)?[ \t]*[:—-]",
        re.IGNORECASE | re.MULTILINE,
    ),
    _phrase(
        "a changelog heading", r"^#{1,6}[ \t]*(?:change[ \t]?log|revision history)\b", re.IGNORECASE | re.MULTILINE
    ),
    _phrase("struck-through text", r"~~[^~\n]+~~"),
    _phrase("'previously'", r"\bpreviously\b"),
    _phrase("'used to'", r"\bused to (?:be|have|do|live|call|return|work|sit|mean)\b"),
    _phrase("'no longer'", r"\bno longer\b"),
    _phrase("'originally'", r"\boriginally\b"),
    _phrase("'we thought'", r"\bwe (?:originally |previously )?thought\b"),
    _phrase("a replacement narrated as history", r"\b(?:was|were|has been|have been|is now|are now) replaced\b"),
    _phrase("'survives as'", r"\bsurvives as\b"),
    _phrase(
        "built-versus-not-built narration",
        r"\b(?:what(?:'s| is) (?:built|implemented)|not (?:yet )?(?:built|implemented))\b",
    ),
    _phrase("a past defect given as the reason", r"\bthe (?:defect|bug|problem|failure) was\b"),
    _phrase("a commit hash offered as a reason", rf"\b(?:commit|in|from|see|since|as of)[ \t]+{_SHA}\b"),
    _phrase("a comparison with a version the reader cannot see", r"\bnow (?:correctly|properly|instead)\b"),
    _phrase(
        "a rename or move narrated as history",
        r"\b(?:was|were) (?:changed|renamed|moved) (?:to|from)\b|\brenamed from\b",
    ),
)

_THE_TEST = (
    "The test in global.md's *What You Write Describes Now, Not the Journey*: would a "
    "reader who never saw the previous version need this sentence? If it only makes sense "
    "to someone who watched the change happen, cut it -- rewrite the artefact so it reads "
    "as though it had always said the new thing. History goes in the commit message, a bd "
    "discussion comment or a close reason. If the phrase genuinely describes present "
    "behaviour rather than a change, stop and ask rather than rewording around this check."
)


def allow(tool_name: str) -> None:
    _hook_log.log_decision("check_diary_prose.py", "PreToolUse", tool_name, "allow")
    sys.exit(0)


def deny(found: Denial, tool_name: str) -> None:
    _hook_log.log_denial("check_diary_prose.py", "PreToolUse", tool_name, found.reason, found.context)
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


def strip_exempt(text: str) -> str:
    """`text` with fenced code blocks and rejected-alternative sections blanked out."""
    without_code = _FENCED_BLOCK_RE.sub("", text)
    return _ALTERNATIVES_RE.sub("", without_code)


class Hit(NamedTuple):
    name: str
    phrase: str
    context: str


class Denial(NamedTuple):
    reason: str
    context: str


def first_hit(text: str) -> Hit | None:
    scannable = strip_exempt(text)
    for pattern in _PATTERNS:
        match = pattern.regex.search(scannable)
        if match is not None:
            context = _hook_log.surrounding_sentence(scannable, match.start(), match.end())
            return Hit(name=pattern.name, phrase=match.group(0).strip(), context=context)
    return None


def denial(text: str, source: str) -> Denial | None:
    hit = first_hit(text)
    if hit is None:
        return None
    reason = f"Blocked: {source} contains {hit.name} -- `{hit.phrase}`. {_THE_TEST}"
    return Denial(reason=reason, context=hit.context)


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


# Flags of `bd remember` that take no value, so the token after them is still positional.
_BD_STANDALONE_FLAGS = frozenset({"--json", "--silent", "--quiet", "--force", "--help", "-h"})


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
    """Denial reason for one `bd ...` invocation, or None if it writes no governed text."""
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


def governed_markdown(file_path: str) -> bool:
    path = Path(file_path)
    return path.suffix.lower() in _MARKDOWN_SUFFIXES and path.name.lower() not in _EXEMPT_FILENAMES


def bash_denial(hook_input: dict) -> Denial | None:
    command = (hook_input.get("tool_input") or {}).get("command", "")
    for call in command_segments(tokenise(command)):
        if call[0] != "bd":
            continue
        found = bd_denial(call)
        if found is not None:
            return found
    return None


def file_denial(tool_name: str, hook_input: dict) -> Denial | None:
    tool_input = hook_input.get("tool_input") or {}
    file_path = tool_input.get("file_path") or ""
    if not governed_markdown(file_path):
        return None
    # Edit: only the replacement is this write's prose; the rest of the file is not.
    text = tool_input.get("content") if tool_name == "Write" else tool_input.get("new_string")
    if not text:
        return None
    return denial(text, f"`{Path(file_path).name}`")


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
