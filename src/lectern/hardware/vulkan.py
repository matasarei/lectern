"""AMD and Intel GPUs, found through the llama.cpp Vulkan build's own device list.

NVML only knows NVIDIA cards. For everything else, `llama-server --list-devices` reports what
the Vulkan backend can use, in one format for every backend (llama.cpp common/arg.cpp):
`  <name>: <description> (<total> MiB, <free> MiB free)`.

Whether a device is integrated, and so shares system RAM, isn't in that line. The Vulkan backend
logs it at debug level as it starts (`ggml_vulkan: 0 = <name> (<driver>) | uma: 1 | ...`,
ggml-vulkan.cpp), which `-v` shows when it comes before `--list-devices`, since that option exits
as soon as it's parsed. The memory can't tell: an 8 GB card in a 16 GB machine has the same
share of RAM as an integrated GPU allowed half of it.
"""

from __future__ import annotations

import functools
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
_TYPE = re.compile(r"ggml_vulkan: (?P<index>\d+) = .*\| uma: (?P<uma>[01]) \|")


@dataclass(frozen=True)
class Device:
    name: str  # e.g. "Vulkan0", "MTL0", "CUDA0", "BLAS"
    description: str  # e.g. "AMD Radeon RX 7600"
    total: int  # bytes
    free: int  # bytes
    uma: bool = False  # integrated: its memory is system RAM. False when the log doesn't say


def parse_devices(output: str) -> list[Device]:
    integrated = {f"Vulkan{m['index']}" for m in _TYPE.finditer(output) if m["uma"] == "1"}
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
                    uma=match["name"].strip() in integrated,
                )
            )
    return devices


def list_devices(server: Path) -> list[Device]:
    try:
        result = subprocess.run(  # noqa: S603 (fixed argv, no shell)
            [str(server), "-v", "--list-devices"], capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.SubprocessError):
        return []
    return parse_devices(result.stdout + result.stderr)


@functools.cache
def cached_devices(server: Path) -> tuple[Device, ...]:
    """The device list for one llama-server build, asked once per process: asking starts
    llama-server and initialises Vulkan, and the GPUs don't change while the app runs."""
    return tuple(list_devices(server))


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
            shared=d.uma,
        )
        for d in devices
        if d.name.lower().startswith("vulkan") and d.total > 0
    ]
