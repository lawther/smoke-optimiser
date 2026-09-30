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
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

_DEFAULT_LOG_DIR = Path.home() / ".agent_rules" / "logs"

# A sentence ends at terminal punctuation followed by whitespace, or at a newline. Requiring the
# whitespace keeps "casey-719.4" and "e.g." from splitting a sentence in the middle.
_SENTENCE_BREAK = re.compile(r"[.!?](?=\s|$)|\n")

_CONTEXT_LINE_CHARS = 80
_CONTEXT_MAX_LINES = 4
_MAX_CONTEXT_CHARS = _CONTEXT_LINE_CHARS * _CONTEXT_MAX_LINES


def _log_dir() -> Path:
    override = os.environ.get("AGENT_RULES_LOG_DIR")
    return Path(override) if override else _DEFAULT_LOG_DIR


def surrounding_sentence(text: str, start: int, end: int) -> str:
    """The sentence containing `text[start:end]`, whitespace-collapsed and capped in length.

    A denial's phrase alone cannot show whether the block was right; the sentence around it can.
    A sentence longer than the cap is windowed around the match rather than cut from the front.
    """
    breaks_before = list(_SENTENCE_BREAK.finditer(text, 0, start))
    sentence_start = breaks_before[-1].end() if breaks_before else 0
    break_after = _SENTENCE_BREAK.search(text, end)
    sentence_end = break_after.end() if break_after else len(text)
    window_start = max(sentence_start, start - _MAX_CONTEXT_CHARS // 2)
    window_end = min(sentence_end, end + _MAX_CONTEXT_CHARS // 2)
    return " ".join(text[window_start:window_end].split())


def _append(fields: dict[str, str | None]) -> None:
    """Best-effort append of one record made of `fields` plus a timestamp and repo. Never raises."""
    try:
        now = datetime.now(timezone.utc)
        record = {"timestamp": now.isoformat(), "repo": Path.cwd().name, **fields}
        log_dir = _log_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        with (log_dir / f"{now:%Y-%m-%d}.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
    except OSError:
        pass


def log_decision(hook: str, event: str, tool_name: str, decision: str, reason: str | None = None) -> None:
    """Best-effort append of one decision record. Never raises."""
    _append(
        {
            "hook": hook,
            "event": event,
            "tool_name": tool_name,
            "decision": decision,
            "reason": reason,
            "context": None,
        }
    )


def log_denial(hook: str, event: str, tool_name: str, reason: str, context: str) -> None:
    """Best-effort append of a denial from a hook that matches on text. Never raises.

    `context` is the sentence that triggered the denial, written to the log verbatim, so it
    holds whatever the agent was writing.
    """
    _append(
        {
            "hook": hook,
            "event": event,
            "tool_name": tool_name,
            "decision": "deny",
            "reason": reason,
            "context": context,
        }
    )


_CLI_MIN_ARGC = 5  # argv[0] + hook, event, tool_name, decision; reason is optional


if __name__ == "__main__":
    # A CLI entry point for hooks written in shell, which have no importable sibling
    # module -- see `block-override-flags.sh`, the only current caller.
    if len(sys.argv) >= _CLI_MIN_ARGC:
        log_decision(*sys.argv[1:5], reason=sys.argv[5] if len(sys.argv) > _CLI_MIN_ARGC else None)
