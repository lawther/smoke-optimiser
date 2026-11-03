#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# dependencies = []
# ///
"""SessionStart: say so when `just update-deps-fleet` has not finished for a week.

update-deps-fleet in agent_rules is the only thing that brings routine dependency upgrades, and
the only way patched Docker base images and Rust toolchains arrive. Nothing runs it on a
schedule, so this reminds the agent, in every enrolled repo, to tell the user when it is
overdue. A week matches the weekly cadence Dependabot version updates had.

The stamp is written by update_deps.py when a fleet run finishes: one ISO 8601 timestamp with
a timezone. A missing or unreadable stamp reads as never run. A fresh one prints nothing.

Standard library only: this file is synced standalone into `.claude/hooks/`, and runs in its
own PEP 723 environment.

A fix-up agent that update_deps.py launches sets AGENT_RULES_NO_STALENESS_NOTICE, so its report
stays about the one bump.
"""

from __future__ import annotations

import datetime as dt
import enum
import json
import os
import sys
from pathlib import Path
from typing import NamedTuple

QUIET_ENV = "AGENT_RULES_NO_STALENESS_NOTICE"
STAMP = Path.home() / ".agent_rules" / "update-deps" / "last-run"
STALE_AFTER = dt.timedelta(weeks=1)


class State(enum.Enum):
    FRESH = "fresh"
    STALE = "stale"
    NEVER = "never"


class Reading(NamedTuple):
    state: State
    age: dt.timedelta


def read(text: str, now: dt.datetime) -> Reading:
    """Judge a stamp's contents. Text that is not an aware ISO timestamp reads as never run."""
    try:
        finished = dt.datetime.fromisoformat(text.strip())
    except ValueError:
        return Reading(State.NEVER, dt.timedelta(0))
    if finished.tzinfo is None:
        return Reading(State.NEVER, dt.timedelta(0))
    age = now - finished
    return Reading(State.STALE if age > STALE_AFTER else State.FRESH, age)


def message(reading: Reading) -> str:
    """What the agent is told, or "" when there is nothing to say."""
    if reading.state is State.FRESH:
        return ""
    when = (
        "has not finished on this machine"
        if reading.state is State.NEVER
        else f"last finished {reading.age.days} days ago"
    )
    return (
        f"Dependency upkeep is overdue: `just update-deps-fleet` in agent_rules {when}. It brings "
        "every routine dependency upgrade and the only patched Docker base images and Rust "
        "toolchains. Tell the user this at the start of your reply, and suggest they run it."
    )


def main() -> int:
    if os.environ.get(QUIET_ENV):
        return 0
    try:
        text = STAMP.read_text(encoding="utf-8")
    except OSError:
        text = ""
    said = message(read(text, dt.datetime.now(dt.UTC)))
    if said:
        output = {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": said}}
        sys.stdout.write(json.dumps(output) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
