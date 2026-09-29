"""What machine are we on: OS, CPU, RAM and the GPU memory models can use.

Each source (NVML, sysctl, psutil) sits behind a small function that tests can replace,
so every branch can be checked without the hardware.
"""

from __future__ import annotations

import platform
import sys
from dataclasses import asdict, dataclass, field
from typing import Literal

import psutil

from lectern.hardware import apple, nvidia, vulkan

GIB = 1024**3
DISPLAY_MARGIN_BYTES = int(0.6 * GIB)  # kept free on a discrete GPU for the desktop

OsName = Literal["linux", "macos", "windows"]
Vendor = Literal["nvidia", "apple", "amd", "intel"]


@dataclass(frozen=True)
class Gpu:
    vendor: Vendor
    name: str
    memory_total: int  # bytes
    memory_free: int | None  # bytes; None where the OS doesn't report it
    unified: bool  # shares system RAM (Apple Silicon)
    cuda_driver: str | None = None  # e.g. "13.0"; NVIDIA only


@dataclass(frozen=True)
class HardwareInfo:
    os: OsName
    arch: str
    cpu: str
    cpu_cores: int
    ram_total: int
    ram_available: int
    gpus: list[Gpu] = field(default_factory=list)
    gpu_budget: int = 0  # bytes a model may use on the GPU; 0 = CPU only

    def to_dict(self) -> dict:
        return asdict(self)


def current_os() -> OsName:
    if sys.platform == "darwin":
        return "macos"
    if sys.platform == "win32":
        return "windows"
    return "linux"


def gpu_budget(gpus: list[Gpu], *, apple_budget: int | None = None) -> int:
    """How much GPU memory models may use: the biggest card minus the display margin,
    or the Apple GPU wired-memory limit on unified memory."""
    if not gpus:
        return 0
    best = max(gpus, key=lambda g: g.memory_total)
    if best.unified:
        return apple_budget or 0
    return max(best.memory_total - DISPLAY_MARGIN_BYTES, 0)


def _installed_vulkan_gpus() -> list[Gpu]:
    """GPUs the llama.cpp Vulkan build can use, if that build is installed."""
    from lectern.config import get_paths
    from lectern.runners.binaries import LlamaCppInstaller

    installer = LlamaCppInstaller(get_paths().runners)
    for variant in ("linux-vulkan", "windows-vulkan"):
        server = installer.server_path(variant)
        if server is not None:
            return vulkan.gpus_from_devices(vulkan.list_devices(server))
    return []


def probe(
    *,
    os_name: OsName | None = None,
    nvidia_probe=nvidia.probe,
    apple_probe=apple.probe,
    vulkan_probe=_installed_vulkan_gpus,
) -> HardwareInfo:
    os_name = os_name or current_os()
    memory = psutil.virtual_memory()
    cpu = platform.processor() or platform.machine()
    gpus: list[Gpu] = []
    apple_budget: int | None = None

    if os_name == "macos":
        mac = apple_probe()
        if mac is not None:
            cpu = mac.chip
            if mac.apple_silicon:
                gpus.append(
                    Gpu(
                        vendor="apple",
                        name=mac.chip,
                        memory_total=mac.ram_total,
                        memory_free=None,
                        unified=True,
                    )
                )
                apple_budget = mac.gpu_budget
    else:
        # NVML knows NVIDIA cards; for AMD and Intel, ask the Vulkan build if it's installed.
        gpus.extend(nvidia_probe() or vulkan_probe())

    return HardwareInfo(
        os=os_name,
        arch=platform.machine(),
        cpu=cpu,
        cpu_cores=psutil.cpu_count(logical=False) or psutil.cpu_count() or 1,
        ram_total=memory.total,
        ram_available=memory.available,
        gpus=gpus,
        gpu_budget=gpu_budget(gpus, apple_budget=apple_budget),
    )
