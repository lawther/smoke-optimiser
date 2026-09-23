"""Tests for reading and writing the profile file both commands share."""

from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from smoke_optimiser.profiler.models import ProfileAnchor, ProfilingData
from smoke_optimiser.profiler.persistence import (
    ProfileFault,
    ProfileUnusableError,
    load_profile,
    read_profile,
    save_profile,
)
from smoke_optimiser.profiler.scope import WHOLE_REPOSITORY


def test_a_saved_profile_loads_back_unchanged(profiled_suite: ProfilingData, tmp_path: Path) -> None:
    profile = tmp_path / ".smoke_profiling_data.json"

    save_profile(profiled_suite, profile)

    assert load_profile(profile, WHOLE_REPOSITORY) == profiled_suite


def test_a_failed_write_leaves_the_previous_profile_intact(profiled_suite: ProfilingData, tmp_path: Path) -> None:
    """A profile is only ever replaced by a complete one.

    The write used to unlink the target first, so anything going wrong after
    that point -- a full disk here, a SIGKILL in the real case -- destroyed a
    good profile and left nothing, or a truncated file, in its place. Downwind
    reads an absent profile as 'run everything' and a truncated one as a hard
    error, and cannot recover the profile it had a moment earlier either way.
    """
    profile = tmp_path / ".smoke_profiling_data.json"
    save_profile(profiled_suite, profile)
    original = profile.read_bytes()

    with (
        patch("smoke_optimiser.profiler.persistence.json.dump", side_effect=OSError("no space left on device")),
        pytest.raises(OSError, match="no space left on device"),
    ):
        save_profile(profiled_suite, profile)

    assert profile.read_bytes() == original, "the previous profile must survive a failed write"
    assert load_profile(profile, WHOLE_REPOSITORY) == profiled_suite, "and must still be readable, not merely present"


def test_a_profile_from_another_directory_of_the_repository_is_refused(
    profiled_suite: ProfilingData, tmp_path: Path
) -> None:
    """The one fault whose every other symptom looks like success.

    Its paths are repository-relative, so a profile recorded in api/ parses
    perfectly when read from the root and simply matches nothing -- the selection
    comes back empty and reads as a change no test reaches. MISPLACED rather than
    MISCONFIGURED because regenerating from here is the entire fix, so the
    ordinary fallback can clear it without a human.
    """
    profile = tmp_path / ".smoke_profiling_data.json"
    save_profile(replace(profiled_suite, anchor=ProfileAnchor(project_offset="api", node_id_prefix="api")), profile)

    with pytest.raises(ProfileUnusableError) as caught:
        read_profile(profile, WHOLE_REPOSITORY)

    assert caught.value.fault is ProfileFault.MISPLACED
    assert "recorded from api" in caught.value.message


def test_a_profile_recorded_where_it_is_read_is_accepted(profiled_suite: ProfilingData, tmp_path: Path) -> None:
    """A worktree is the same path space, so the check must not be about the checkout.

    An absolute recorded root would make copying a profile into a fresh worktree
    an error. The offset is relative precisely so that it does not.
    """
    profile = tmp_path / ".smoke_profiling_data.json"
    save_profile(replace(profiled_suite, anchor=ProfileAnchor(project_offset="api", node_id_prefix="api")), profile)

    assert read_profile(profile, "api").anchor.node_id_prefix == "api"
