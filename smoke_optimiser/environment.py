import os
import platform
import socket
from dataclasses import dataclass

try:
    import psutil
except ImportError:
    psutil = None  # type: ignore[assignment]


@dataclass(frozen=True)
class MachineEnvironment:
    """Captured details about the machine environment."""

    os: str | None
    os_version: str | None
    platform: str | None
    architecture: str | None
    cpu_model: str | None
    cpu_cores_physical: int | None
    cpu_cores_logical: int | None
    ram_total_mb: int | None
    ram_available_mb: int | None
    hostname: str | None


def _get_cpu_model() -> str | None:
    """Best-effort CPU model name retrieval."""
    if platform.system() == "Darwin":
        # On macOS, we can use sysctl
        try:
            return os.popen("sysctl -n machdep.cpu.brand_string").read().strip()
        except (OSError, ValueError):
            return None
    elif platform.system() == "Linux":
        # On Linux, parse /proc/cpuinfo
        try:
            with open("/proc/cpuinfo") as f:
                for line in f:
                    if "model name" in line:
                        return line.split(":")[1].strip()
        except (OSError, IndexError):
            pass
    return None


def capture_environment() -> MachineEnvironment:
    """Capture machine environment information."""
    cpu_cores_logical = os.cpu_count()
    cpu_cores_physical = None
    ram_total_mb = None
    ram_available_mb = None

    if psutil:
        try:
            cpu_cores_physical = psutil.cpu_count(logical=False)
            virtual_mem = psutil.virtual_memory()
            ram_total_mb = virtual_mem.total // (1024 * 1024)
            ram_available_mb = virtual_mem.available // (1024 * 1024)
        except (OSError, AttributeError):
            pass

    return MachineEnvironment(
        os=platform.system(),
        os_version=platform.release(),
        platform=platform.platform(),
        architecture=platform.machine(),
        cpu_model=_get_cpu_model(),
        cpu_cores_physical=cpu_cores_physical,
        cpu_cores_logical=cpu_cores_logical,
        ram_total_mb=ram_total_mb,
        ram_available_mb=ram_available_mb,
        hostname=socket.gethostname(),
    )
