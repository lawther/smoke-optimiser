#!/usr/bin/env python3
"""PreToolUse(Bash) guard: keep each bead's `model:` label present, single and honoured.

Every bead in this project carries exactly one `model:sonnet` or `model:opus` label
recording which model the work was scoped for -- `model:sonnet` for mechanical work,
`model:opus` for work that needs design decisions. This hook enforces that rule at the
two points where it can be broken.

CREATE. `bd create` (and its `new` alias, and `bd q`) must produce exactly one model
label. Labels are inherited from `--parent` unless `--no-inherit-labels` is passed, so
the effective set is the explicit `--labels` plus the parent's -- which is why an
explicit label that disagrees with an inherited one yields two, not an override.

CLAIM. `bd update <id> --claim` is the only atomic claim path, so the hook watches for
that flag combination, reads each target's model label via `bd show`, and compares it
to the model that produced the current turn.

Both checks treat two model labels as a failure, not as "either will do". A bead
labelled for both families silently satisfies the claim comparison for every model,
which is strictly worse than an unlabelled bead: unlabelled fails open by design and
visibly, dual-labelled looks guarded while guarding nothing.

No env var or hook input field exposes the active model directly, so it is read from
the transcript instead: `transcript_path` (given in the hook payload) is a JSONL file
where each assistant turn's `message.model` records what actually generated it. That
is the model of record -- more reliable than anything the agent could self-report.

A failure denies the command, which surfaces the reason to the agent so it stops and
asks the user rather than proceeding. Everything else -- no model label on the bead at
claim time, no resolvable transcript, a `bd show` failure, a batch create from a file
or graph whose labels cannot be attributed per issue -- allows the command through:
this is a policy nudge, not a safety boundary, and a false block costs more than an
occasional unlabelled bead slipping past it.

`bd` DETECTION IS COMMAND-POSITION, NOT MEMBERSHIP. `create_commands()` and
`claim_commands()` split the token stream on bare `&&`/`||`/`;`/`|`, treat a segment as
a `bd` invocation only when `bd` is that segment's first token, and check every such
segment in the chain rather than just the first. This closes two concrete bugs a plain
`"bd" in tokens` / `tokens.index("bd")` check has: `rg bd create` was denied (false
positive -- "bd" there is an argument to `rg`, not the command), and `bd show x && bd
create y` was allowed (false negative -- `.index` only ever finds the first "bd", so a
later command in the chain was never examined).

Segmenting on shell operators is still NOT a complete fix, and does not attempt to be:
`FOO=1 bd create`, `sh -c "bd create"`, `time`/`command`/`nohup` prefixes, `(bd
create)` subshells, `if`/loop bodies, and `xargs` all still evade command-position
detection, because none of them put `bd` as the segment's first token even though `bd`
is what ultimately runs. Closing those needs interposing on `bd` itself (a wrapper on
PATH) rather than parsing the shell string -- tracked as a follow-up, deliberately not
attempted here. This hook remains a nudge, not a boundary.

Tokenising and command-segmenting are shared with the other Bash guards -- see
`_shell_tokenise.py` for how a newline, a heredoc body and a multi-line quoted value are
told apart, and for the fail-open policy `tokenise()` applies here: an unparseable tail is
dropped rather than checked, consistent with the rest of this hook (see `main`'s
exception guard) -- this is a policy nudge, not a safety boundary, so a false block costs
more than an occasional unlabelled bead slipping past it.

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
import subprocess
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

_MODEL_LABEL_RE = re.compile(r"^model:(?P<family>\w+)$")
_MODEL_FAMILY_RE = re.compile(r"opus|sonnet|haiku|fable", re.IGNORECASE)
_BD_SHOW_TIMEOUT_SECS = 15

# Subcommands of `bd` that mint a new issue. `new` is an alias of `create`; `q` is
# quick capture, which takes --labels too.
_CREATE_SUBCOMMANDS = frozenset({"create", "new", "q"})

# Create forms whose labels cannot be attributed to individual issues: the labels live
# inside the markdown/JSON payload, not in argv. These are allowed through unchecked.
_BATCH_CREATE_FLAGS = frozenset({"-f", "--file", "--graph"})

_LABEL_FLAGS = frozenset({"-l", "--labels"})
_PARENT_FLAGS = frozenset({"--parent"})

_HOW_TO_LABEL = (
    "Every bead needs exactly one model label: `model:sonnet` for mechanical work, "
    "`model:opus` for work needing design decisions. Add `-l model:<family>`."
)


class ClaimCommand(NamedTuple):
    issue_ids: list[str]


class CreateCommand(NamedTuple):
    explicit_labels: list[str]
    parent_id: str
    inherits_labels: bool


class Mismatch(NamedTuple):
    issue_id: str
    wanted_families: list[str]


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


def model_families(labels: list[str]) -> set[str]:
    """The distinct model families named by `labels` (e.g. {'opus'})."""
    return {match.group("family").lower() for label in labels if (match := _MODEL_LABEL_RE.match(label.strip()))}


def create_commands(tokens: list[str]) -> list[CreateCommand]:
    """Every checkable create this (possibly chained) command performs."""
    creates = []
    for call in bd_segments(tokens):
        subcommand = next((token for token in call[1:] if not token.startswith("-")), "")
        if subcommand not in _CREATE_SUBCOMMANDS:
            continue
        # --dry-run and --help/-h create nothing, and batch forms carry their labels in a
        # payload file.
        if (
            "--dry-run" in call
            or "--help" in call
            or "-h" in call
            or any(token.partition("=")[0] in _BATCH_CREATE_FLAGS for token in call)
        ):
            continue
        explicit_labels = [
            label for value in flag_values(call, _LABEL_FLAGS) for label in value.split(",") if label.strip()
        ]
        parent_ids = flag_values(call, _PARENT_FLAGS)
        creates.append(
            CreateCommand(
                explicit_labels=explicit_labels,
                parent_id=parent_ids[0] if parent_ids else "",
                inherits_labels="--no-inherit-labels" not in call,
            )
        )
    return creates


def create_denial(create: CreateCommand) -> str | None:
    """Why this create must be blocked, or None if its model labelling is correct."""
    families = model_families(create.explicit_labels)
    inherited: set[str] = set()
    if create.parent_id and create.inherits_labels:
        parent_families = bead_model_labels([create.parent_id]).get(create.parent_id)
        if parent_families is None:
            # Parent unreadable: cannot compute the effective set, so do not guess.
            return None
        inherited = set(parent_families)
    effective = families | inherited

    if not effective:
        return f"Blocked: this `bd create` would make a bead with no model label. {_HOW_TO_LABEL}"
    if len(effective) == 1:
        return None

    conflict = "/".join(f"model:{family}" for family in sorted(effective))
    if inherited and families - inherited:
        return (
            f"Blocked: this `bd create` would make a bead labelled {conflict} — the explicit label does "
            f"not override the one inherited from {create.parent_id}, it is added to it. Pass "
            "`--no-inherit-labels` and give the full label set explicitly, or drop the explicit model "
            "label to keep the parent's. A bead with two model labels satisfies the claim-time guard "
            "for every model, so it is not guarded at all."
        )
    return (
        f"Blocked: this `bd create` names {conflict}. A bead carries exactly one model label — two "
        "satisfies the claim-time guard for every model, so it is not guarded at all."
    )


def claim_commands(tokens: list[str]) -> list[ClaimCommand]:
    """Every `bd update ... --claim` this (possibly chained) command performs."""
    claims = []
    for call in bd_segments(tokens):
        if "update" not in call or "--claim" not in call:
            continue
        update_idx = call.index("update")
        issue_ids = []
        for token in call[update_idx + 1 :]:
            if token.startswith("-"):
                break
            issue_ids.append(token)
        if issue_ids:
            claims.append(ClaimCommand(issue_ids=issue_ids))
    return claims


def current_model_family(transcript_path: str) -> str | None:
    """Model family (e.g. 'sonnet') of the most recent assistant turn in the transcript."""
    try:
        with Path(transcript_path).open(encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return None
    for raw_line in reversed(lines):
        stripped_line = raw_line.strip()
        if not stripped_line:
            continue
        try:
            # Standard-library-only script (see module docstring); every field access below
            # is via .get() with a None-safe fallback, so a malformed transcript line degrades
            # to "no model found" rather than raising.
            entry = json.loads(stripped_line)  # noqa: ML400
        except json.JSONDecodeError:
            continue
        if entry.get("type") != "assistant":
            continue
        model_id = (entry.get("message") or {}).get("model")
        if not model_id:
            continue
        match = _MODEL_FAMILY_RE.search(model_id)
        if match:
            return match.group(0).lower()
    return None


def bead_model_labels(issue_ids: list[str]) -> dict[str, list[str]]:
    """Map of issue_id -> its model:<family> label values (e.g. ['opus']), missing if unknown."""
    try:
        # issue_ids come from shlex-tokenised argv positions (never a raw shell string), passed
        # as separate argv elements with shell=False; "bd" is resolved via PATH deliberately,
        # matching how the SessionStart hook already invokes it project-wide.
        result = subprocess.run(
            ["bd", "show", *issue_ids, "--json"],
            capture_output=True,
            text=True,
            timeout=_BD_SHOW_TIMEOUT_SECS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {}
    issues = payload if isinstance(payload, list) else [payload]
    labels_by_id = {}
    for issue in issues:
        issue_id = issue.get("id")
        if not issue_id:
            continue
        families = [
            match.group("family").lower()
            for label in issue.get("labels") or []
            if (match := _MODEL_LABEL_RE.match(label))
        ]
        labels_by_id[issue_id] = families
    return labels_by_id


def main() -> None:
    # Standard-library-only script (see module docstring); every field access below is via
    # .get() with a None-safe fallback, and the __main__ guard below fails open on any
    # exception, so malformed hook input degrades to "allow" rather than raising.
    hook_input = json.load(sys.stdin)  # noqa: ML400
    command = (hook_input.get("tool_input") or {}).get("command", "")
    tokens = tokenise(command)

    for create in create_commands(tokens):
        denial = create_denial(create)
        if denial is not None:
            deny(denial)
            return

    issue_ids = [issue_id for claim in claim_commands(tokens) for issue_id in claim.issue_ids]
    if not issue_ids:
        allow()
        return

    labels_by_id = bead_model_labels(issue_ids)
    ambiguous = [
        Mismatch(issue_id, wanted) for issue_id in issue_ids if len(wanted := labels_by_id.get(issue_id) or []) > 1
    ]
    if ambiguous:
        details = "; ".join(f"{m.issue_id} is labelled {'/'.join(sorted(m.wanted_families))}" for m in ambiguous)
        deny(
            f"Blocked: {details}. A bead carries exactly one model label — two matches every model, so "
            f"the tier is unguarded. Fix the bead's labels before claiming it. {_HOW_TO_LABEL}"
        )
        return

    # An unlabelled bead is a deliberate fail-open (see module docstring), as is an
    # unreadable transcript -- neither can be compared against, so neither blocks.
    current_family = current_model_family(hook_input.get("transcript_path", ""))
    if current_family is None:
        allow()
        return

    mismatches = [
        Mismatch(issue_id, wanted_families)
        for issue_id in issue_ids
        if (wanted_families := labels_by_id.get(issue_id)) and current_family not in wanted_families
    ]
    if not mismatches:
        allow()
        return

    details = "; ".join(f"{m.issue_id} wants {'/'.join(m.wanted_families)}" for m in mismatches)
    deny(
        f"Blocked: this session is running on '{current_family}', but {details}. Stop and ask "
        "the user whether to switch model, reassign the bead's model label, or claim it anyway."
    )


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001 - a hook bug must fail open, not block every Bash call
        allow()
