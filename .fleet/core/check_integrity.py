# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pydantic>=2.12",
# ]
# ///
"""Fail when anything the fleet's sync recorded in .fleet/manifest.json has changed or gone.

The manifest names, for each family, every file and every marked block (in the justfile,
or in the rules file) that sync wrote into this repo, with the sha256 of the bytes it
wrote, and every Claude Code hook registration it owns in .claude/settings.json. This
script reads only this repo, so it runs the same in the git hook and in CI.

A failure means a managed copy was edited in place, or deleted. Make the change in
agent_rules, under the payload path the block header or manifest names, and sync it back.

The models below are the manifest's only definition: agent_rules' sync writes the file by
loading this module, so the writer and the checker cannot disagree about its shape.
"""

from __future__ import annotations

import argparse
import enum
import hashlib
import json
import pathlib
import stat
import sys
from collections.abc import Sequence
from typing import Literal, NamedTuple, NewType

import pydantic

MANIFEST_PATH = pathlib.Path(".fleet") / "manifest.json"
SETTINGS_PATH = pathlib.Path(".claude") / "settings.json"
SCHEMA = 1

FamilyName = NewType("FamilyName", str)


class _Record(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid", frozen=True)


class FileRecord(_Record):
    """A whole file sync wrote, relative to the repo root."""

    path: str
    sha256: str
    executable: bool


class BlockRecord(_Record):
    """A marked block sync wrote inside a file the repo otherwise owns.

    The hash covers the block from the first character of `begin` to the last of `end`.
    """

    path: str
    begin: str
    end: str
    sha256: str


class Registration(_Record):
    """One hook entry sync owns in .claude/settings.json. An empty matcher is no matcher."""

    event: str
    matcher: str
    command: str


class FamilyRecord(_Record):
    """What one family's sync wrote, and the agent_rules revision it wrote it from."""

    source: str
    files: tuple[FileRecord, ...] = ()
    blocks: tuple[BlockRecord, ...] = ()
    registrations: tuple[Registration, ...] = ()


class Manifest(_Record):
    """The whole of .fleet/manifest.json."""

    schema_version: Literal[1] = pydantic.Field(alias="schema")
    families: dict[FamilyName, FamilyRecord]

    model_config = pydantic.ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    def dumps(self) -> str:
        """The manifest as sync writes it: stable key order, one trailing newline."""
        return json.dumps(self.model_dump(mode="json", by_alias=True), indent=2, sort_keys=True) + "\n"


class Problem(enum.Enum):
    """Why one recorded item fails. The value is what the report prints."""

    MISSING = "missing"
    CHANGED = "changed since sync wrote it"
    MODE_CHANGED = "executable bit changed since sync wrote it"
    BLOCK_MISSING = "block markers not found"
    BLOCK_CHANGED = "block changed since sync wrote it"
    UNREGISTERED = "registration missing from .claude/settings.json"


class Finding(NamedTuple):
    """One recorded item that no longer matches, and the family that recorded it."""

    family: FamilyName
    subject: str
    problem: Problem


class Span(NamedTuple):
    """Where a block sits in its file's text."""

    start: int
    end: int


class NoBlock(enum.Enum):
    """The marker pair is not in the text."""

    ABSENT = enum.auto()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def is_executable(path: pathlib.Path) -> bool:
    return bool(path.stat().st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH))


def find_block(text: str, begin: str, end: str) -> Span | NoBlock:
    """The span from `begin` through `end` in `text`, or ABSENT if either marker is missing."""
    start = text.find(begin)
    if start < 0:
        return NoBlock.ABSENT
    finish = text.find(end, start + len(begin))
    if finish < 0:
        return NoBlock.ABSENT
    return Span(start, finish + len(end))


def block_text(text: str, begin: str, end: str) -> str | NoBlock:
    """The block's text, markers included, as its recorded hash covers it."""
    span = find_block(text, begin, end)
    if isinstance(span, NoBlock):
        return span
    return text[span.start : span.end]


def load(root: pathlib.Path) -> Manifest:
    return Manifest.model_validate_json((root / MANIFEST_PATH).read_text(encoding="utf-8"))


def _check_file(root: pathlib.Path, family: FamilyName, record: FileRecord) -> list[Finding]:
    path = root / record.path
    if not path.is_file():
        return [Finding(family, record.path, Problem.MISSING)]
    if sha256_file(path) != record.sha256:
        return [Finding(family, record.path, Problem.CHANGED)]
    if is_executable(path) != record.executable:
        return [Finding(family, record.path, Problem.MODE_CHANGED)]
    return []


def _check_block(root: pathlib.Path, family: FamilyName, record: BlockRecord) -> list[Finding]:
    subject = f"{record.path}: {record.begin}"
    path = root / record.path
    if not path.is_file():
        return [Finding(family, subject, Problem.MISSING)]
    found = block_text(path.read_text(encoding="utf-8"), record.begin, record.end)
    if isinstance(found, NoBlock):
        return [Finding(family, subject, Problem.BLOCK_MISSING)]
    if sha256_text(found) != record.sha256:
        return [Finding(family, subject, Problem.BLOCK_CHANGED)]
    return []


def registered(settings: object) -> frozenset[Registration]:
    """Every hook entry in a settings.json document, in the shape the manifest records."""
    found: set[Registration] = set()
    hooks = settings.get("hooks", {}) if isinstance(settings, dict) else {}
    if not isinstance(hooks, dict):
        return frozenset()
    for event, blocks in hooks.items():
        for block in blocks if isinstance(blocks, list) else []:
            if not isinstance(block, dict):
                continue
            matcher = str(block.get("matcher", ""))
            for entry in block.get("hooks", []):
                if isinstance(entry, dict) and isinstance(entry.get("command"), str):
                    found.add(Registration(event=event, matcher=matcher, command=entry["command"]))
    return frozenset(found)


def _check_registrations(root: pathlib.Path, family: FamilyName, records: Sequence[Registration]) -> list[Finding]:
    if not records:
        return []
    path = root / SETTINGS_PATH
    try:
        present = registered(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        present = frozenset()
    return [
        Finding(family, f"{record.event} {record.matcher!r} {record.command}", Problem.UNREGISTERED)
        for record in records
        if record not in present
    ]


def check(root: pathlib.Path, manifest: Manifest) -> list[Finding]:
    """Every recorded item under `root` that no longer matches the manifest."""
    findings: list[Finding] = []
    for family, record in sorted(manifest.families.items()):
        for file in record.files:
            findings.extend(_check_file(root, family, file))
        for block in record.blocks:
            findings.extend(_check_block(root, family, block))
        findings.extend(_check_registrations(root, family, record.registrations))
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("root", nargs="?", type=pathlib.Path, default=pathlib.Path(), help="repo root (default: .)")
    root = parser.parse_args().root
    try:
        manifest = load(root)
    except FileNotFoundError:
        print(f"FAIL  {MANIFEST_PATH} not found: this repo has not been synced by agent_rules", file=sys.stderr)
        return 1
    except pydantic.ValidationError as error:
        print(f"FAIL  {MANIFEST_PATH} is not a manifest sync wrote:\n{error}", file=sys.stderr)
        return 1
    findings = check(root, manifest)
    for finding in findings:
        print(f"FAIL  [{finding.family}] {finding.subject}: {finding.problem.value}", file=sys.stderr)
    if findings:
        print(
            "\nA fleet-managed copy was changed here. Make the change in agent_rules' payload and sync it,"
            " or restore the copy with git.",
            file=sys.stderr,
        )
        return 1
    print(
        f"ok    {sum(len(r.files) + len(r.blocks) + len(r.registrations) for r in manifest.families.values())}"
        " fleet-managed items match .fleet/manifest.json"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
