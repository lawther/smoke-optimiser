#!/usr/bin/env -S uv run --script
# Fleet-managed copy of fleet payload/claude_hooks/check_bead_model.py. Do not edit it here:
#      edit it there, then run `just sync` in fleet.
# /// script
# requires-python = ">=3.14"
# dependencies = []
# ///
"""PreToolUse(Bash) guard: keep each bead's `model:` label present, single and honoured.

Every bead in this project carries exactly one model label recording which model the
work was scoped for: `model:haiku` for rote, fully specified work, `model:sonnet` for
mechanical work that still needs some judgement, `model:opus` for work that needs design
decisions. This hook enforces that rule at the two points where it can be broken.

CREATE. `bd create` (and its `new` alias, and `bd q`) must produce exactly one model
label, naming one of the families in `ModelFamily`. An unknown family -- a typo such as
`model:haku` -- is denied here, because no agent could ever claim the bead it makes.
Labels are inherited from `--parent` unless `--no-inherit-labels` is passed, so the
effective set is the explicit `--labels` plus the parent's -- which is why an explicit
label that disagrees with an inherited one yields two, not an override.

CLAIM. `bd update <id> --claim` is the only atomic claim path, so the hook watches for
that flag combination, reads each target's model label via `bd show`, and compares it
to the model that produced the current turn.

Both checks treat two model labels as a failure, not as "either will do". A bead
labelled for both families silently satisfies the claim comparison for every model,
which is strictly worse than an unlabelled bead: unlabelled fails open by design and
visibly, dual-labelled looks guarded while guarding nothing.

No env var or hook input field exposes the active model directly, so it is read from
the issuing agent's transcript instead: a JSONL file where each assistant turn's
`message.model` records what actually generated it. That is the model of record -- more
reliable than anything the agent could self-report. Which transcript depends on who
issued the command. The payload's `transcript_path` is always the top-level session's,
even for a command a subagent runs, so it is the right file only when the payload has
no `agent_id`. Claude Code sets `agent_id` when, and only when, the hook fires inside a
subagent, and that subagent's own transcript sits beside the session's:
`<session>.jsonl` has its subagents at `<session>/subagents/agent-<agent_id>.jsonl`. A
subagent's model is read from that file and never from the session's assistant turns,
because the session's model is the wrong answer for it, not a weaker one.

The assistant entry for the tool call being checked is not in the transcript when the
hook runs, so a subagent whose first tool call is the claim has no turn of its own on
record. For that case the session's transcript holds the subagent's launch record: a
background launch writes a `toolUseResult` carrying the subagent's `agentId` and the
`resolvedModel` Claude Code picked for it (after the Agent call's model input, agent
frontmatter and the configured subagent default) before the subagent starts. The
launch record is read only when the subagent's own transcript has no model, and only
the entry naming this `agent_id` counts. Both layouts are Claude Code's, not
documented; `just check-subagent-spawn-model` in fleet replays this fallback
against real transcripts so that a layout change fails a check rather than quietly
reinstating the denial below.

A claim of a labelled bead whose issuing agent has no model on record by either route
is denied, not allowed: a foreground subagent has no launch record until it finishes,
and a layout change removes it. The denial tells the agent to make a separate tool call
first and retry, and the retry is then checked against the real model.

A failure denies the command, which surfaces the reason to the agent so it stops and
asks the user rather than proceeding. Everything else -- no model label on the bead at
claim time, a `bd show` failure, a batch create from a file or graph whose labels cannot
be attributed per issue -- allows the command through: this is a policy nudge, not a
safety boundary, and a false block costs more than an occasional unlabelled bead
slipping past it.

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

Standard library only, and no third-party imports: the hook runs in its own PEP 723 environment
(`dependencies = []`), not the project's, so no project package is importable. `_shell_tokenise`
is a sibling file in this same directory, always synced alongside this one (see family.toml), so
it is not a third-party dependency -- but it is loaded by path (`_load_shell_tokenise()`) rather
than a plain `import _shell_tokenise`: this file is synced standalone into `.fleet/claude_hooks/` in
several repos, and some run a static type checker whose module-resolution roots do not include
that directory, so a literal import naming a sibling module reads as unresolved there even
though the runtime import (Python always puts a script's own directory on `sys.path`) works
fine. Loading by path has no import name for a static checker to flag, which avoids asking every
consuming repo to configure around this file.
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from types import ModuleType


# Loading a sibling would otherwise leave a __pycache__ beside the delivered copies, an
# untracked directory in .fleet/ that nothing owns.
sys.dont_write_bytecode = True


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

_MODEL_LABEL_RE = re.compile(r"^model:(?P<family>\w+)$")
_MODEL_FAMILY_RE = re.compile(r"opus|sonnet|haiku|fable", re.IGNORECASE)
_MODEL_SWITCH_RE = re.compile(r"<local-command-stdout>Set model to `(?P<model>[^`]*)`")
# An agent_id becomes part of a file path, so only the characters Claude Code uses for it
# are accepted; anything else could name a file outside the subagents directory.
_AGENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_SUBAGENTS_DIR = "subagents"
_ASYNC_LAUNCHED = "async_launched"
_BD_SHOW_TIMEOUT_SECS = 15

# Subcommands of `bd` that mint a new issue. `new` is an alias of `create`; `q` is
# quick capture, which takes --labels too.
_CREATE_SUBCOMMANDS = frozenset({"create", "new", "q"})

# Create forms whose labels cannot be attributed to individual issues: the labels live
# inside the markdown/JSON payload, not in argv. These are allowed through unchecked.
_BATCH_CREATE_FLAGS = frozenset({"-f", "--file", "--graph"})

_LABEL_FLAGS = frozenset({"-l", "--labels"})
_PARENT_FLAGS = frozenset({"--parent"})


class ModelFamily(StrEnum):
    """The model families a bead's `model:` label may name."""

    HAIKU = "haiku"
    SONNET = "sonnet"
    OPUS = "opus"


_HOW_TO_LABEL = (
    "Every bead needs exactly one model label: `model:haiku` for rote, fully specified work "
    "(the bead says exactly what to change and how to verify it), `model:sonnet` for mechanical "
    "work that still needs some judgement, `model:opus` for work needing design decisions. "
    "Add `-l model:<family>`."
)


class ClaimCommand(NamedTuple):
    issue_ids: list[str]


class CreateCommand(NamedTuple):
    explicit_labels: list[str]
    parent_id: str
    inherits_labels: bool


class IssuingAgent(NamedTuple):
    """The agent that issued the command, and the transcript holding its model of record."""

    description: str
    transcript_path: str
    # The session transcript holding a subagent's launch record; empty for the session itself,
    # which has no launch record.
    launch_record_path: str
    agent_id: str


class Mismatch(NamedTuple):
    issue_id: str
    wanted_families: list[str]


def allow() -> None:
    _hook_log.log_decision("check_bead_model.py", "PreToolUse", "Bash", "allow")
    sys.exit(0)


def deny(reason: str) -> None:
    _hook_log.log_decision("check_bead_model.py", "PreToolUse", "Bash", "deny", reason)
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
    known = {str(family) for family in ModelFamily}
    if unknown := families - known:
        named = ", ".join(f"model:{family}" for family in sorted(unknown))
        return f"Blocked: this `bd create` names unknown model family {named}. {_HOW_TO_LABEL}"
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


def _switch_family(entry: dict) -> str | None:
    """The family a `/model` command output entry switched to, or None if it is not one."""
    if entry.get("type") != "user":
        return None
    content = (entry.get("message") or {}).get("content")
    if not isinstance(content, str):
        return None
    switched = _MODEL_SWITCH_RE.search(content)
    if switched is None:
        return None
    match = _MODEL_FAMILY_RE.search(switched.group("model"))
    return match.group(0).lower() if match else None


def current_model_family(transcript_path: str) -> str | None:
    """Model family (e.g. 'sonnet') in effect: the latest assistant turn or `/model` switch.

    The assistant entry for the tool call being checked is not in the transcript yet when
    this hook runs, so right after a `/model` switch the latest assistant turn is still the
    previous model's. The switch itself is recorded, so whichever of the two is newer wins.
    """
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
        if switched_family := _switch_family(entry):
            return switched_family
        if entry.get("type") != "assistant":
            continue
        model_id = (entry.get("message") or {}).get("model")
        if not model_id:
            continue
        match = _MODEL_FAMILY_RE.search(model_id)
        if match:
            return match.group(0).lower()
    return None


def issuing_agent(hook_input: dict) -> IssuingAgent:
    """Who issued this command: the top-level session, or the subagent named by `agent_id`."""
    session_transcript = hook_input.get("transcript_path") or ""
    agent_id = hook_input.get("agent_id") or ""
    if not agent_id:
        return IssuingAgent(
            description="this session", transcript_path=session_transcript, launch_record_path="", agent_id=""
        )
    description = f"subagent {agent_id}"
    if not session_transcript or not _AGENT_ID_RE.match(agent_id):
        # No transcript can be named for this subagent. An empty path reads as "no model on
        # record", which denies the claim -- the session's transcript is never substituted.
        return IssuingAgent(description=description, transcript_path="", launch_record_path="", agent_id="")
    session_dir = Path(session_transcript).with_suffix("")
    subagent_transcript = session_dir / _SUBAGENTS_DIR / f"agent-{agent_id}.jsonl"
    return IssuingAgent(
        description=description,
        transcript_path=str(subagent_transcript),
        launch_record_path=session_transcript,
        agent_id=agent_id,
    )


def launch_record_family(session_transcript: str, agent_id: str) -> str | None:
    """Model family Claude Code resolved for subagent `agent_id`, from its launch record."""
    if not session_transcript or not agent_id:
        return None
    try:
        with Path(session_transcript).open(encoding="utf-8") as f:
            lines = [line for line in f if agent_id in line]
    except OSError:
        return None
    for raw_line in lines:
        try:
            # Standard-library-only script (see module docstring); every field access below
            # is type-checked, so a malformed line degrades to "no launch record".
            entry = json.loads(raw_line)  # noqa: ML400
        except json.JSONDecodeError:
            continue
        result = entry.get("toolUseResult") if isinstance(entry, dict) else None
        if not isinstance(result, dict):
            continue
        if result.get("agentId") != agent_id or result.get("status") != _ASYNC_LAUNCHED:
            continue
        resolved = result.get("resolvedModel")
        match = _MODEL_FAMILY_RE.search(resolved) if isinstance(resolved, str) else None
        return match.group(0).lower() if match else None
    return None


def agent_model_family(agent: IssuingAgent) -> str | None:
    """The issuing agent's model family: its own transcript first, then its launch record."""
    return current_model_family(agent.transcript_path) or launch_record_family(agent.launch_record_path, agent.agent_id)


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
    except OSError, subprocess.TimeoutExpired:
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

    # An unlabelled bead is a deliberate fail-open (see module docstring): with nothing to
    # compare against, the issuing agent's model is not even looked up.
    if not any(labels_by_id.get(issue_id) for issue_id in issue_ids):
        allow()
        return

    agent = issuing_agent(hook_input)
    current_family = agent_model_family(agent)
    if current_family is None:
        deny(
            f"Blocked: the model of {agent.description} is not on record yet, so this claim cannot be "
            "checked against the bead's model label. A model is recorded once the agent has completed "
            "a tool call. Make a separate tool call first (for example `bd show "
            f"{issue_ids[0]}`), then retry the claim. A chained command such as `bd show "
            f"{issue_ids[0]} && bd update {issue_ids[0]} --claim` is a single tool call and does not count."
        )
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
    claim_anyway = ", ".join(f"`! bd update {m.issue_id} --claim`" for m in mismatches)
    deny(
        f"Blocked: {agent.description} is running on '{current_family}', but {details}. Stop and ask "
        "the user whether to switch model, delegate the bead to an agent on the wanted model, "
        "reassign the bead's model label, or claim it anyway. To claim it anyway, the user runs the "
        f"claim command for each bead at the Claude Code prompt: {claim_anyway}. The `!` runs it in "
        "the user's own shell, which this hook does not check."
    )


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001 - a hook bug must fail open, not block every Bash call
        allow()
