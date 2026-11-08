#!/usr/bin/env -S uv run --script
# Fleet-managed copy of fleet payload/github/check_dependabot_security.py. Do not edit it here:
#      edit it there, then run `just sync` in fleet.
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pydantic>=2.12",
#     "pyyaml>=6",
# ]
# ///
"""Fail if this repo's dependabot.yml is not configured for security updates only.

Two things are checked. First, security updates for an ecosystem open one pull request per
package when no group applies to them. A group with `applies-to: security-updates` collects
them into one pull request per ecosystem and directory. This reports every lockfile, and a repo
with workflow files, whose ecosystem and directory is not covered by such a group, so a repo
cannot quietly lack one. GitHub Actions have no lockfile; workflow files stand in for one, and
Dependabot reads them from `/`.

The repo owns its dependabot.yml, which carries deliberate content, so nothing is written. A
repo opts an ecosystem out with `dependabot_exempt` in the `[github]` section of its fleet.toml.
A repo with no GitHub origin remote has no Dependabot and is skipped.

Second, every entry that can open version-update pull requests. The fleet takes only security
updates from Dependabot: an entry turns version updates off with `open-pull-requests-limit: 0`,
which leaves its security updates open, and carries no group that applies to version updates.
Routine upgrades, base-image digests and Rust toolchains included, come from fleet'
update-deps instead.

The repository's "Grouped security updates" toggle overrides the file's groups when on, and
has no REST endpoint, so this cannot see it.

This script judges the repo at the working directory, or at the repo root given as its only
argument.
"""

from __future__ import annotations

import argparse
import enum
import fnmatch
import importlib
import pathlib
import re
import subprocess
import sys
from typing import NamedTuple

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

# Importing a sibling would otherwise leave a __pycache__ beside the delivered copies, an
# untracked directory in .fleet/ that nothing owns.
sys.dont_write_bytecode = True
_CORE = pathlib.Path(__file__).resolve().parents[1] / "core"
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))
fleet_config = importlib.import_module("fleet_config")

_GITHUB_REMOTE = re.compile(
    r"^(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)[^/]+/[^/]+?(?:\.git)?/?$"
)

_DEPENDABOT_FILE = pathlib.PurePosixPath(".github/dependabot.yml")
_WORKFLOWS_DIR = pathlib.PurePosixPath(".github/workflows")
_WORKFLOW_SUFFIXES = frozenset({".yml", ".yaml"})
_ACTIONS_ECOSYSTEM = "github-actions"
_VERSION_UPDATES_OFF = 0
# GitHub's documented default when an entry does not set open-pull-requests-limit.
_DEFAULT_OPEN_PULL_REQUESTS_LIMIT = 5

_LOCKFILE_NAMES = {
    "uv.lock": "uv",
    "package-lock.json": "npm",
    "yarn.lock": "npm",
    "pnpm-lock.yaml": "npm",
    "Cargo.lock": "cargo",
}


class AppliesTo(enum.Enum):
    VERSION_UPDATES = "version-updates"
    SECURITY_UPDATES = "security-updates"


class _Group(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    applies_to: AppliesTo = Field(default=AppliesTo.VERSION_UPDATES, alias="applies-to")


class _Update(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    package_ecosystem: str = Field(alias="package-ecosystem")
    directory: str = ""
    directories: list[str] = []
    groups: dict[str, _Group] = {}
    open_pull_requests_limit: int = Field(default=_DEFAULT_OPEN_PULL_REQUESTS_LIMIT, alias="open-pull-requests-limit")

    @property
    def location(self) -> str:
        return ",".join(directory for directory in [self.directory, *self.directories] if directory)


class _Config(BaseModel):
    model_config = ConfigDict(extra="ignore")

    updates: list[_Update] = []


class Verdict(enum.Enum):
    COVERED = "ok"
    EXEMPT = "exempt"
    NO_FILE = "no dependabot.yml"
    NO_ENTRY = "no entry for this ecosystem and directory"
    NO_SECURITY_GROUP = "entry has no security-updates group"
    BAD_FILE = "dependabot.yml is not valid"


class EntryVerdict(enum.Enum):
    SECURITY_ONLY = "security updates only"
    OPENS_VERSION_UPDATES = "opens version updates: set open-pull-requests-limit: 0"
    VERSION_UPDATES_GROUP = "has a group that applies to version updates"


class EntryFinding(NamedTuple):
    ecosystem: str
    location: str
    verdict: EntryVerdict

    @property
    def failed(self) -> bool:
        return self.verdict is not EntryVerdict.SECURITY_ONLY


class Lockfile(NamedTuple):
    path: pathlib.PurePosixPath
    ecosystem: str

    @property
    def directory(self) -> str:
        if self.ecosystem == _ACTIONS_ECOSYSTEM:
            return "/"
        parent = self.path.parent
        return "/" if parent == pathlib.PurePosixPath(".") else f"/{parent}"


class Finding(NamedTuple):
    lockfile: Lockfile
    verdict: Verdict

    @property
    def failed(self) -> bool:
        return self.verdict not in (Verdict.COVERED, Verdict.EXEMPT)


def lockfiles(tracked: list[str]) -> list[Lockfile]:
    """The lockfiles among `tracked` paths, with the Dependabot ecosystem each belongs to.

    The first workflow file stands in for the repo's GitHub Actions usage: one entry at `/`
    covers all of them, so one finding per repo is enough.
    """
    found = []
    has_workflows = False
    for name in tracked:
        path = pathlib.PurePosixPath(name)
        ecosystem = _LOCKFILE_NAMES.get(path.name)
        if ecosystem is not None:
            found.append(Lockfile(path, ecosystem))
        elif path.parent == _WORKFLOWS_DIR and path.suffix in _WORKFLOW_SUFFIXES and not has_workflows:
            has_workflows = True
            found.append(Lockfile(_WORKFLOWS_DIR, _ACTIONS_ECOSYSTEM))
    return found


def _covers(update: _Update, directory: str) -> bool:
    candidates = [update.directory, *update.directories]
    return any(fnmatch.fnmatchcase(directory, pattern) for pattern in candidates if pattern)


def judge(lockfile: Lockfile, config: _Config) -> Verdict:
    """Whether `config` has a security-updates group on an entry covering `lockfile`."""
    entries = [
        update
        for update in config.updates
        if update.package_ecosystem == lockfile.ecosystem and _covers(update, lockfile.directory)
    ]
    if not entries:
        return Verdict.NO_ENTRY
    for entry in entries:
        if any(group.applies_to is AppliesTo.SECURITY_UPDATES for group in entry.groups.values()):
            return Verdict.COVERED
    return Verdict.NO_SECURITY_GROUP


def judge_entry(update: _Update) -> EntryVerdict:
    """Whether `update` opens only security updates."""
    if update.open_pull_requests_limit != _VERSION_UPDATES_OFF:
        return EntryVerdict.OPENS_VERSION_UPDATES
    if any(group.applies_to is AppliesTo.VERSION_UPDATES for group in update.groups.values()):
        return EntryVerdict.VERSION_UPDATES_GROUP
    return EntryVerdict.SECURITY_ONLY


def load_config(text: str) -> _Config:
    return _Config.model_validate(yaml.safe_load(text) or {})


def check_repo(root: pathlib.Path, exempt: frozenset[str], tracked: list[str]) -> list[Finding]:
    """One finding per lockfile in the repo."""
    found = lockfiles(tracked)
    dependabot_file = root / _DEPENDABOT_FILE
    if not dependabot_file.is_file():
        return [
            Finding(lockfile, Verdict.EXEMPT if lockfile.ecosystem in exempt else Verdict.NO_FILE) for lockfile in found
        ]
    try:
        config = load_config(dependabot_file.read_text(encoding="utf-8"))
    except (yaml.YAMLError, ValidationError):
        return [
            Finding(lockfile, Verdict.EXEMPT if lockfile.ecosystem in exempt else Verdict.BAD_FILE)
            for lockfile in found
        ]
    return [
        Finding(lockfile, Verdict.EXEMPT if lockfile.ecosystem in exempt else judge(lockfile, config))
        for lockfile in found
    ]


def check_entries(root: pathlib.Path) -> list[EntryFinding]:
    """One finding per updates entry. A missing or invalid file has no entries to judge;
    `check_repo` reports it."""
    dependabot_file = root / _DEPENDABOT_FILE
    if not dependabot_file.is_file():
        return []
    try:
        config = load_config(dependabot_file.read_text(encoding="utf-8"))
    except (yaml.YAMLError, ValidationError):
        return []
    return [EntryFinding(update.package_ecosystem, update.location, judge_entry(update)) for update in config.updates]


def has_github_origin(root: pathlib.Path) -> bool:
    """Whether the repo at `root` pushes to a github.com `origin`, the only place Dependabot runs."""
    completed = subprocess.run(
        ["git", "-C", str(root), "remote", "get-url", "origin"], capture_output=True, text=True, check=False
    )
    return completed.returncode == 0 and _GITHUB_REMOTE.match(completed.stdout.strip()) is not None


def tracked_files(root: pathlib.Path) -> list[str]:
    """The paths git tracks in the repo at `root`."""
    completed = subprocess.run(["git", "-C", str(root), "ls-files"], capture_output=True, text=True, check=True)
    return completed.stdout.splitlines()


def run(root: pathlib.Path) -> int:
    """Judge the repo at `root`, print what is wrong with it, and return the exit status."""
    try:
        exempt = frozenset(ecosystem.value for ecosystem in fleet_config.load(root).github.dependabot_exempt)
    except fleet_config.FleetConfigError as error:
        print(f"FAIL  {fleet_config.FLEET_TOML}: {'; '.join(error.problems)}")
        return 1
    if not has_github_origin(root):
        print("ok    skipped: no GitHub origin remote")
        return 0

    findings = check_repo(root, exempt, tracked_files(root))
    entry_findings = check_entries(root)
    for finding in findings:
        if finding.failed:
            print(
                f"FAIL  {finding.lockfile.path}: {finding.lockfile.ecosystem} {finding.lockfile.directory}:"
                f" {finding.verdict.value}. Add a security-updates group in .github/dependabot.yml,"
                " or list the ecosystem under dependabot_exempt in the [github] section of fleet.toml"
            )
    for entry in entry_findings:
        if entry.failed:
            print(
                f"FAIL  {_DEPENDABOT_FILE}: entry {entry.ecosystem} {entry.location}: {entry.verdict.value}."
                " Set open-pull-requests-limit: 0 and keep only security-updates groups"
            )
    if any(finding.failed for finding in findings) or any(entry.failed for entry in entry_findings):
        return 1
    print(f"ok    {_DEPENDABOT_FILE}: security updates only, and every lockfile covered")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("root", nargs="?", type=pathlib.Path, default=pathlib.Path(), help="repo root (default: .)")
    return run(parser.parse_args().root)


if __name__ == "__main__":
    sys.exit(main())
