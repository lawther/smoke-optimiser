#!/usr/bin/env -S uv run --script
# Fleet-managed copy of fleet payload/lint/check_python_version.py. Do not edit it here:
#      edit it there, then run `just sync` in fleet.
# /// script
# requires-python = ">=3.14"
# dependencies = [
#     "pydantic>=2.12",
# ]
# ///
"""Fail when this repo disagrees with its own root .python-version about which Python it runs.

The root `.python-version` is the one statement of the Python this repo runs. fleet's Python
floor sync writes it, and everything else must follow it, because each of these lets the repo
lint, test or ship against a different Python from the one it claims and nothing else notices:

  - a tracked pyproject.toml with a [project] table whose requires-python is not exactly ">="
    and that version
  - a uv project in a subdirectory without its own .python-version equal to the root one: uv
    looks for the file no higher than the project it runs in
  - a setup-python step in a workflow that hard-codes python-version rather than reading
    python-version-file
  - a ruff.toml target-version that, with ruff_shared.toml's as the fallback, does not name that
    version. A repo on the fleet's floor has no override (it would only repeat the shared value);
    a repo pinned below the floor needs one
  - a ty.toml that carries a python-version, which ty would read in place of requires-python
  - a "Programming Language :: Python :: 3.X" classifier, which restates requires-python and
    drifts from it. The generic "Programming Language :: Python :: 3" is fine

It never reads the date: whether the version is the fleet's floor is fleet's own `fleet check
python-floor`. Workflows are scanned line by line, because the payload carries pydantic and no
YAML library; a setup-python step is the lines from its `- ` to the next line at that indent or
less.

This script judges the repo at the working directory, or at the repo root given as its only
argument. The ruff and ty checks look in the directory the manifest records the shared lint
config in, and say nothing when the manifest does not record one: check_lint_config.py reports that.
"""

from __future__ import annotations

import argparse
import enum
import importlib
import pathlib
import re
import subprocess
import sys
import tomllib
from collections.abc import Sequence
from typing import NamedTuple

from pydantic import BaseModel, ConfigDict, Field, ValidationError

# Importing a sibling would otherwise leave a __pycache__ beside the delivered copies, an
# untracked directory in .fleet/ that nothing owns.
sys.dont_write_bytecode = True
_HERE = pathlib.Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
check_lint_config = importlib.import_module("check_lint_config")

PYTHON_VERSION_FILE = pathlib.PurePosixPath(".python-version")
PYPROJECT_NAME = "pyproject.toml"
LOCKFILE_NAME = "uv.lock"
SHARED_RUFF = "ruff_shared.toml"
TY_CONFIG = "ty.toml"
WORKFLOWS_DIR = pathlib.PurePosixPath(".github/workflows")
WORKFLOW_SUFFIXES = frozenset({".yml", ".yaml"})
SETUP_PYTHON = "actions/setup-python@"

_VERSION = re.compile(r"(?P<major>\d+)\.(?P<minor>\d+)")
_SETUP_PYTHON_LINE = re.compile(rf"^(?P<lead>\s*(?:-\s+)?)uses:\s*['\"]?{re.escape(SETUP_PYTHON)}")
_STEP_START = re.compile(r"^(?P<indent>\s*)-\s")
_PYTHON_VERSION_KEY = re.compile(r"^\s*(?:-\s+)?python-version\s*:\s*(?P<value>.*?)\s*(?:#.*)?$")
_MINOR_CLASSIFIER = re.compile(r"^Programming Language :: Python :: 3\.\d+(?:\s|$)")


class Problem(enum.Enum):
    """Why one thing about the repo's Python version fails."""

    UNREADABLE = enum.auto()  # a file this check must understand cannot be read
    NO_VERSION_FILE = enum.auto()  # the root .python-version is missing or is not a minor version
    REQUIRES_PYTHON = enum.auto()  # requires-python is not ">=" plus the version
    SUBPROJECT_VERSION_FILE = enum.auto()  # a uv project's own .python-version is missing or differs
    HARD_CODED_WORKFLOW = enum.auto()  # setup-python is given python-version, not python-version-file
    RUFF_TARGET = enum.auto()  # the effective ruff target-version does not name the version
    REDUNDANT_RUFF_OVERRIDE = enum.auto()  # a ruff.toml target-version that repeats the shared one
    TY_PYTHON_VERSION = enum.auto()  # ty.toml carries a python-version
    CLASSIFIER = enum.auto()  # a classifier names a minor version


class Finding(NamedTuple):
    """One thing wrong with a repo's Python version: the file it is about, the cause, and what to do."""

    subject: pathlib.PurePosixPath
    problem: Problem
    detail: str


class HardCoded(NamedTuple):
    """A python-version given to a setup-python step: the workflow line it is on and what it says."""

    line: int
    value: str


class StepStart(NamedTuple):
    """Where a workflow step opens: its line index and the column of its `- `."""

    index: int
    column: int


class Version(NamedTuple):
    """A Python minor version as .python-version writes it."""

    major: int
    minor: int

    @property
    def dotted(self) -> str:
        return f"{self.major}.{self.minor}"

    @property
    def requires_python(self) -> str:
        return f">={self.dotted}"

    @property
    def ruff_target(self) -> str:
        return f"py{self.major}{self.minor}"


class _Project(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    requires_python: str = Field(default="", alias="requires-python")
    classifiers: list[str] = Field(default_factory=list)


class _Pyproject(BaseModel):
    model_config = ConfigDict(extra="ignore")

    project: _Project = Field(default_factory=_Project)

    @property
    def is_project(self) -> bool:
        return "project" in self.model_fields_set


class _RuffConfig(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    target_version: str = Field(default="", alias="target-version")


class _TyEnvironment(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    python_version: object = Field(default="", alias="python-version")

    @property
    def sets_python_version(self) -> bool:
        return "python_version" in self.model_fields_set


class _TyConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    environment: _TyEnvironment = Field(default_factory=_TyEnvironment)


class GitError(Exception):
    """git could not list the repo's tracked files."""


def tracked_files(root: pathlib.Path) -> list[pathlib.PurePosixPath]:
    """The paths git tracks in the repo at `root`."""
    completed = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        message = f"git cannot list the files in {root}: {completed.stderr.strip()}"
        raise GitError(message)
    return sorted(pathlib.PurePosixPath(name) for name in completed.stdout.split("\0") if name)


def _read_model[Model: BaseModel](
    root: pathlib.Path, path: pathlib.PurePosixPath, model: type[Model]
) -> Model | Finding:
    try:
        return model.model_validate(tomllib.loads((root / path).read_text(encoding="utf-8")))
    except tomllib.TOMLDecodeError, UnicodeDecodeError, ValidationError:
        return Finding(path, Problem.UNREADABLE, "not valid TOML of the shape this check reads")


def root_version(root: pathlib.Path) -> Version | Finding:
    """The Python version the root .python-version names, or why it does not name one."""
    file = root / PYTHON_VERSION_FILE
    if not file.is_file():
        return Finding(
            PYTHON_VERSION_FILE,
            Problem.NO_VERSION_FILE,
            "missing: it is the one statement of the Python this repo runs",
        )
    text = file.read_text(encoding="utf-8").strip()
    match = _VERSION.fullmatch(text)
    if match is None:
        return Finding(
            PYTHON_VERSION_FILE, Problem.NO_VERSION_FILE, f"holds {text!r}, not a minor version such as '3.14'"
        )
    return Version(int(match["major"]), int(match["minor"]))


def check_pyprojects(root: pathlib.Path, tracked: Sequence[pathlib.PurePosixPath], version: Version) -> list[Finding]:
    """requires-python, classifiers and uv projects' own .python-version, in every tracked pyproject with a [project]."""
    findings: list[Finding] = []
    for path in tracked:
        if path.name != PYPROJECT_NAME or not (root / path).is_file():
            continue
        parsed = _read_model(root, path, _Pyproject)
        if isinstance(parsed, Finding):
            findings.append(parsed)
            continue
        if not parsed.is_project:
            continue
        project = parsed.project
        if project.requires_python != version.requires_python:
            findings.append(
                Finding(
                    path,
                    Problem.REQUIRES_PYTHON,
                    f"requires-python is {project.requires_python!r}, not {version.requires_python!r} "
                    f"({PYTHON_VERSION_FILE} says {version.dotted})",
                )
            )
        findings.extend(
            Finding(
                path,
                Problem.CLASSIFIER,
                f"classifier {classifier!r} names a minor version. requires-python is the one statement "
                f'of supported versions; use "Programming Language :: Python :: 3" or drop it',
            )
            for classifier in project.classifiers
            if _MINOR_CLASSIFIER.match(classifier)
        )
        findings.extend(_check_subproject_version_file(root, tracked, path, version))
    return findings


def _check_subproject_version_file(
    root: pathlib.Path, tracked: Sequence[pathlib.PurePosixPath], pyproject: pathlib.PurePosixPath, version: Version
) -> list[Finding]:
    directory = pyproject.parent
    if directory == pathlib.PurePosixPath() or directory / LOCKFILE_NAME not in tracked:
        return []
    own = directory / PYTHON_VERSION_FILE
    if not (root / own).is_file():
        return [
            Finding(
                own,
                Problem.SUBPROJECT_VERSION_FILE,
                f"missing: uv does not look above {directory}/ for one, so this project would build on "
                f"whatever Python is newest rather than {version.dotted}",
            )
        ]
    held = (root / own).read_text(encoding="utf-8").strip()
    if held != version.dotted:
        return [
            Finding(
                own,
                Problem.SUBPROJECT_VERSION_FILE,
                f"holds {held!r}, not {version.dotted!r} ({PYTHON_VERSION_FILE} at the repo root)",
            )
        ]
    return []


def _step_end(lines: Sequence[str], start: int, dash_column: int) -> int:
    """The index of the first line after the step that begins at `start`, whose `- ` is at `dash_column`."""
    for index in range(start + 1, len(lines)):
        line = lines[index]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if len(line) - len(line.lstrip()) <= dash_column:
            return index
    return len(lines)


def _step_start(lines: Sequence[str], uses_index: int) -> StepStart:
    """The line index and column of the `- ` that opens the step whose `uses:` is at `uses_index`."""
    own = _STEP_START.match(lines[uses_index])
    if own is not None:
        return StepStart(uses_index, len(own["indent"]))
    uses_column = len(lines[uses_index]) - len(lines[uses_index].lstrip())
    for index in range(uses_index - 1, -1, -1):
        opener = _STEP_START.match(lines[index])
        if opener is not None and len(opener["indent"]) < uses_column:
            return StepStart(index, len(opener["indent"]))
    return StepStart(uses_index, 0)


def hard_coded_versions(text: str) -> list[HardCoded]:
    """The line number and value of every python-version given to a setup-python step in `text`."""
    lines = text.splitlines()
    found: dict[int, HardCoded] = {}
    for index, line in enumerate(lines):
        if not _SETUP_PYTHON_LINE.match(line):
            continue
        start = _step_start(lines, index)
        for step_index in range(start.index, _step_end(lines, start.index, start.column)):
            key = _PYTHON_VERSION_KEY.match(lines[step_index])
            if key is not None:
                found[step_index + 1] = HardCoded(step_index + 1, key["value"])
    return sorted(found.values())


def check_workflows(root: pathlib.Path, tracked: Sequence[pathlib.PurePosixPath]) -> list[Finding]:
    """Every setup-python step in a tracked workflow that hard-codes python-version."""
    findings: list[Finding] = []
    for path in tracked:
        if path.parent != WORKFLOWS_DIR or path.suffix not in WORKFLOW_SUFFIXES or not (root / path).is_file():
            continue
        try:
            text = (root / path).read_text(encoding="utf-8")
        except UnicodeDecodeError:
            findings.append(Finding(path, Problem.UNREADABLE, "not valid UTF-8"))
            continue
        findings.extend(
            Finding(
                path,
                Problem.HARD_CODED_WORKFLOW,
                f"line {hard_coded.line}: setup-python sets python-version: {hard_coded.value}. Use `python-version-file: "
                f"{PYTHON_VERSION_FILE}` so CI runs the Python the repo declares",
            )
            for hard_coded in hard_coded_versions(text)
        )
    return findings


def check_ruff_target(root: pathlib.Path, config_dir: pathlib.PurePosixPath, version: Version) -> list[Finding]:
    """The ruff target-version the repo ends up with, which ruff_shared.toml supplies unless ruff.toml overrides it."""
    shared_path = config_dir / SHARED_RUFF
    if not (root / shared_path).is_file():
        return []
    shared = _read_model(root, shared_path, _RuffConfig)
    if isinstance(shared, Finding):
        return [shared]
    own_path = check_lint_config.ruff_config_path(root / config_dir)
    override = ""
    if own_path is not None:
        own_relative = config_dir / own_path.name
        own = _read_model(root, own_relative, _RuffConfig)
        if isinstance(own, Finding):
            return [own]
        override = own.target_version
        if override and override == shared.target_version:
            return [
                Finding(
                    own_relative,
                    Problem.REDUNDANT_RUFF_OVERRIDE,
                    f"target-version = {override!r} repeats {SHARED_RUFF}. A repo on the fleet's floor has no "
                    f"override; delete it",
                )
            ]
        if override:
            return _target_mismatch(own_relative, override, version)
    return _target_mismatch(shared_path, shared.target_version, version)


def _target_mismatch(subject: pathlib.PurePosixPath, held: str, version: Version) -> list[Finding]:
    if held == version.ruff_target:
        return []
    return [
        Finding(
            subject,
            Problem.RUFF_TARGET,
            f"ruff target-version is {held!r}, not {version.ruff_target!r} ({PYTHON_VERSION_FILE} says "
            f"{version.dotted}). A repo pinned below the fleet's floor overrides it in ruff.toml",
        )
    ]


def check_ty(root: pathlib.Path, config_dir: pathlib.PurePosixPath) -> list[Finding]:
    """A finding when the repo's ty.toml carries a python-version."""
    path = config_dir / TY_CONFIG
    if not (root / path).is_file():
        return []
    parsed = _read_model(root, path, _TyConfig)
    if isinstance(parsed, Finding):
        return [parsed]
    if not parsed.environment.sets_python_version:
        return []
    return [
        Finding(
            path,
            Problem.TY_PYTHON_VERSION,
            f"[environment] python-version = {parsed.environment.python_version!r} takes the place of "
            f"requires-python. Delete it, so ty follows the project",
        )
    ]


def lint_config_dir(root: pathlib.Path) -> pathlib.PurePosixPath | None:
    """Where the manifest says the shared lint config was delivered, relative to `root`, or None if it says nothing."""
    try:
        found = check_lint_config.delivered_config_dir(root)
    except FileNotFoundError, check_lint_config.check_integrity.pydantic.ValidationError:
        return None
    if found is None:
        return None
    return pathlib.PurePosixPath(found.relative_to(root).as_posix())


def judge(root: pathlib.Path) -> list[Finding]:
    """What is wrong with the Python version of the repo at `root`; empty when nothing."""
    version = root_version(root)
    if isinstance(version, Finding):
        return [version]
    tracked = tracked_files(root)
    findings = [*check_pyprojects(root, tracked, version), *check_workflows(root, tracked)]
    config_dir = lint_config_dir(root)
    if config_dir is not None:
        findings.extend([*check_ruff_target(root, config_dir, version), *check_ty(root, config_dir)])
    return findings


def run(root: pathlib.Path) -> int:
    """Judge the repo at `root`, print what is wrong with it, and return the exit status."""
    try:
        findings = judge(root)
    except GitError as error:
        print(f"FAIL  {error}")
        return 1
    for finding in findings:
        print(f"FAIL  {finding.subject}: {finding.detail}")
    if findings:
        return 1
    print(f"ok    everything that names a Python version agrees with {PYTHON_VERSION_FILE}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("root", nargs="?", type=pathlib.Path, default=pathlib.Path(), help="repo root (default: .)")
    return run(parser.parse_args().root)


if __name__ == "__main__":
    sys.exit(main())
