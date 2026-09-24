#!/usr/bin/env bash
# PreToolUse(Bash) guard: refuse commands whose only purpose is to override a refusal.
#
# Rationale: when bd refuses to close a blocked issue, or git refuses a non-fast-forward
# push, or a precommit gate fails, that refusal is the tool reporting something true. The
# override flags exist to silence it, and silencing is a decision for the human, not the
# agent — so this hook denies the command and makes the agent come back and ask.
#
# This wrapper does the plumbing only: pull the command string out of the hook payload and
# hand it to the decision script as a plain argument. The payload never reaches Python as
# structured data, so nothing here has to trust a shape it did not check.
#
# Exit 0 always; a denial is communicated via the PreToolUse JSON contract on stdout.

set -uo pipefail

# Parameter expansion rather than dirname: no subprocess on a hook that runs before every
# single Bash call, and it still resolves if PATH is broken.
here=${0%/*}

deny() {
    printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":%s}}' "$1"
    exit 0
}

if ! command -v jq >/dev/null 2>&1; then
    # Fail closed: a guard that cannot run must not look like a guard that found nothing.
    deny '"Blocked: the override-flag guard could not run because jq is not installed, so this command was not checked. Install jq, or ask the user before proceeding."'
fi

command=$(jq -r '.tool_input.command // empty')
[[ -z $command ]] && exit 0

exec python3 "$here/block_override_flags.py" "$command"
