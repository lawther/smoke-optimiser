import dataclasses

import pytest

from smoke_optimiser.environment import MachineEnvironment, capture_environment


def test_capture_environment() -> None:
    env = capture_environment()
    assert isinstance(env, MachineEnvironment)
    assert env.os is not None
    assert env.hostname is not None
    # Logical cores should always be detectable
    assert env.cpu_cores_logical is not None
    assert env.cpu_cores_logical > 0


def test_capture_environment_immutability() -> None:
    env = capture_environment()
    assert dataclasses.is_dataclass(env)

    # A better way is trying to set an attribute
    with pytest.raises(AttributeError):
        env.os = "OtherOS"  # type: ignore[misc]
