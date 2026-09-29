"""AMD and Intel GPUs, found through the llama.cpp Vulkan build's own device list.

NVML only knows NVIDIA cards. For everything else, `llama-server --list-devices` reports what
the Vulkan backend can use, in one format for every backend (llama.cpp common/arg.cpp):
`  <name>: <description> (<total> MiB, <free> MiB free)`.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from lectern.hardware.probe import Gpu

MIB = 1024**2
_LINE = re.compile(
    r"^\s+(?P<name>[^:]+):\s+(?P<desc>.*)\s+\((?P<total>\d+) MiB, (?P<free>\d+) MiB free\)\s*$"
)


@dataclass(frozen=True)
class Device:
    name: str  # e.g. "Vulkan0", "MTL0", "CUDA0", "BLAS"
    description: str  # e.g. "AMD Radeon RX 7600"
    total: int  # bytes
    free: int  # bytes


def parse_devices(output: str) -> list[Device]:
    devices = []
    for line in output.splitlines():
        match = _LINE.match(line)
        if match:
            devices.append(
                Device(
                    name=match["name"].strip(),
                    description=match["desc"].strip(),
                    total=int(match["total"]) * MIB,
                    free=int(match["free"]) * MIB,
                )
            )
    return devices


def list_devices(server: Path) -> list[Device]:
    try:
        result = subprocess.run(  # noqa: S603 (fixed argv, no shell)
            [str(server), "--list-devices"], capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.SubprocessError):
        return []
    return parse_devices(result.stdout + result.stderr)


def _vendor(description: str) -> str:
    text = description.lower()
    if "nvidia" in text or "geforce" in text or "rtx" in text:
        return "nvidia"
    if "intel" in text or "arc" in text.split():
        return "intel"
    return "amd"


def gpus_from_devices(devices: list[Device]) -> list[Gpu]:
    """Only Vulkan devices with memory count; the CPU and BLAS entries aren't GPUs."""
    from lectern.hardware.probe import Gpu

    return [
        Gpu(
            vendor=_vendor(d.description),  # type: ignore[arg-type]
            name=d.description,
            memory_total=d.total,
            memory_free=d.free,
            unified=False,
        )
        for d in devices
        if d.name.lower().startswith("vulkan") and d.total > 0
    ]
