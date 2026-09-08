"""Which changed paths mean the whole environment moved, whatever the maps say.

A lockfile, a dependency declaration or a container definition changes how every
test in the suite behaves, and yet nothing opens one while the suite runs -- so
the read map records no read of it, and would answer for a changed uv.lock with
"nothing depends on this". That answer is measured and wrong: the dependency it
pins was read by the interpreter before the suite started, through machinery no
audit hook of ours was installed for.

So these paths are a fixed rule that runs ahead of the maps. The list is
defaults plus whatever a project adds, which is safe to configure in a way an
IGNORE list would not be: every entry can only ever ADD a full-suite run, so a
wrong entry costs time rather than correctness.

Patterns are matched with fnmatch against the repository-relative path and
against the bare filename, so "uv.lock" catches one at any depth while
"deploy/*.tf" catches only those.
"""

from __future__ import annotations

from fnmatch import fnmatch
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

DEFAULT_ENVIRONMENT_FILES: tuple[str, ...] = (
    "uv.lock",
    "poetry.lock",
    "Pipfile.lock",
    "requirements*.txt",
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "tox.ini",
    "pytest.ini",
    "conftest.cfg",
    ".python-version",
    "Dockerfile",
    "Dockerfile.*",
    "docker-compose*.yml",
    "docker-compose*.yaml",
)
"""What changes the behaviour of a whole suite in every project we have seen.

Deliberately not a judgement about any one repository: a project that has more
adds them, and one that has fewer pays nothing for an entry that matches no
file it contains.
"""


def is_environment_file(path: str, patterns: Iterable[str]) -> bool:
    """Does this changed path define the environment the suite runs in?"""
    name = path.rsplit("/", maxsplit=1)[-1]
    return any(fnmatch(path, pattern) or fnmatch(name, pattern) for pattern in patterns)
