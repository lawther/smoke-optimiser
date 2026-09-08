"""Tests for reading and writing the profile file both commands share."""

from pathlib import Path
from unittest.mock import patch

import pytest

from smoke_optimiser.profiler.models import ProfilingData
from smoke_optimiser.profiler.persistence import load_profile, save_profile


def test_a_saved_profile_loads_back_unchanged(profiled_suite: ProfilingData, tmp_path: Path) -> None:
    profile = tmp_path / ".smoke_profiling_data.json"

    save_profile(profiled_suite, profile)

    assert load_profile(profile) == profiled_suite


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
    assert load_profile(profile) == profiled_suite, "and must still be readable, not merely present"
