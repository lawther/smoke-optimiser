#!/usr/bin/env python3
"""Shared decision logger for the fleet's synced hooks.

Every hook's `allow()`/`deny()` calls `log_decision()` here, so each fired decision is
recorded to `~/.agent_rules/logs/<date>.jsonl` -- one file per day, one JSON object per
line. The location is deliberately outside every enrolled repo (these hooks run across
all of them, so a per-repo log would scatter the record the same way per-repo hook
registration used to) and outside `~/.claude` (that tree is Claude Code's own, not this
fleet's). `AGENT_RULES_LOG_DIR` overrides the directory, which is how tests -- and
nothing else -- avoid writing into the real log.

Never raises: a logging failure must not turn a working hook into a blocked tool call,
so `log_decision()` swallows every exception itself rather than relying on callers to
wrap it. Standard library only, and loaded by path rather than a plain sibling import --
see `check_diary_prose.py`'s module docstring for why: this file is synced standalone
into several repos' `.claude/hooks/`, and some run a static type checker whose
module-resolution roots don't include that directory.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

_DEFAULT_LOG_DIR = Path.home() / ".agent_rules" / "logs"


def _log_dir() -> Path:
    override = os.environ.get("AGENT_RULES_LOG_DIR")
    return Path(override) if override else _DEFAULT_LOG_DIR


def log_decision(hook: str, event: str, tool_name: str, decision: str, reason: str | None = None) -> None:
    """Best-effort append of one decision record. Never raises."""
    try:
        now = datetime.now(UTC)
        record = {
            "timestamp": now.isoformat(),
            "repo": Path.cwd().name,
            "hook": hook,
            "event": event,
            "tool_name": tool_name,
            "decision": decision,
            "reason": reason,
        }
        log_dir = _log_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        with (log_dir / f"{now:%Y-%m-%d}.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
    except OSError:
        pass


_CLI_MIN_ARGC = 5  # argv[0] + hook, event, tool_name, decision; reason is optional


if __name__ == "__main__":
    # A CLI entry point for hooks written in shell, which have no importable sibling
    # module -- see `block-override-flags.sh`, the only current caller.
    if len(sys.argv) >= _CLI_MIN_ARGC:
        log_decision(*sys.argv[1:5], reason=sys.argv[5] if len(sys.argv) > _CLI_MIN_ARGC else None)
