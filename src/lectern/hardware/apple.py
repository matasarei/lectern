"""Apple Silicon: unified memory, where the GPU may use part of system RAM.

macOS caps GPU-wired memory at about 2/3 of RAM on machines with 36 GB or less and 3/4
above that, unless the user set `iogpu.wired_limit_mb`.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass

GIB = 1024**3
MIB = 1024**2
SMALL_MAC_LIMIT = 36 * GIB


@dataclass(frozen=True)
class MacInfo:
    chip: str
    apple_silicon: bool
    ram_total: int
    wired_limit_mb: int  # 0 = macOS default

    @property
    def gpu_budget(self) -> int:
        if self.wired_limit_mb > 0:
            return self.wired_limit_mb * MIB
        fraction = 2 / 3 if self.ram_total <= SMALL_MAC_LIMIT else 3 / 4
        return int(self.ram_total * fraction)


def _sysctl(name: str) -> str | None:
    try:
        result = subprocess.run(  # noqa: S603 (fixed argv, no shell)
            ["/usr/sbin/sysctl", "-n", name],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip()


def probe(sysctl: Callable[[str], str | None] = _sysctl) -> MacInfo | None:
    memsize = sysctl("hw.memsize")
    if memsize is None:
        return None
    return MacInfo(
        chip=sysctl("machdep.cpu.brand_string") or "Mac",
        apple_silicon=sysctl("hw.optional.arm64") == "1",
        ram_total=int(memsize),
        wired_limit_mb=int(sysctl("iogpu.wired_limit_mb") or 0),
    )
