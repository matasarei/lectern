"""stable-diffusion.cpp builds, for image models: pick the right one for this machine, download
it once, unpack it safely.

Pinned apart from llama.cpp (runners/binaries.py), so either one updates without the other.
Each asset's size and sha256 come from the GitHub release data for that tag
(`gh api repos/leejet/stable-diffusion.cpp/releases/tags/<tag>`), so installing needs no
GitHub API call, only the download.

An image is made by running `sd-cli` once per request (modules/image/generate.py): it has no
port, so nothing to protect with a key, and its memory comes back when it exits. The release
also has `sd-server`, which has no API key; it isn't used.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
from collections.abc import Callable
from pathlib import Path

import httpx2

from lectern.downloads.fetch import DownloadError, fetch_verified
from lectern.hardware.probe import HardwareInfo
from lectern.runners.binaries import (
    Asset,
    UnsupportedPlatform,
    _default_client,
    _gather_libraries,
    _has_vulkan,
    _unpack,
)

SD_CPP_BUILD = "master-1053-5a1ba5d"
_COMMIT = SD_CPP_BUILD.rsplit("-", 1)[1]
RELEASE_URL = f"https://github.com/leejet/stable-diffusion.cpp/releases/download/{SD_CPP_BUILD}"


def _a(name: str, size: int, sha256: str) -> Asset:
    return Asset(name.format(c=_COMMIT), size, sha256)


# Sizes and sha256 from the release data for master-1053-5a1ba5d (2026-11-07). There is no
# Linux CUDA build: NVIDIA cards on Linux run the Vulkan one. The ROCm builds aren't used yet.
VARIANTS: dict[str, tuple[Asset, ...]] = {
    "macos-arm64": (
        _a(
            "sd-master-{c}-bin-Darwin-macOS-26.7.2-arm64.zip",
            37747685,
            "b2d5fd9b9757371d27189772952a8b357e873b3e3c99c179ddeb3214aeeb9142",
        ),
    ),
    "linux-vulkan": (
        _a(
            "sd-master-{c}-bin-Linux-Ubuntu-26.04-x86_64-vulkan.zip",
            41680635,
            "e03878a031fe2feaf35ac2a656bf5f3785de3fc75e1fef1d765839897260fcc1",
        ),
    ),
    "linux-cpu": (
        _a(
            "sd-master-{c}-bin-Linux-Ubuntu-26.04-x86_64.zip",
            28804112,
            "c3793b52b8a6ecaaffd3030cc05e20ed764b635c121ab4272a5da42c0cf6d8c4",
        ),
    ),
    "windows-cuda12": (
        _a(
            "sd-master-{c}-bin-win-cuda12-x64.zip",
            363293031,
            "971e2925d98ca3176686108e7e19f1d883a269d9becb6aae28ff03837d1b1a63",
        ),
        _a(
            "cudart-sd-bin-win-cu12-x64.zip",
            563452046,
            "fe20366827d357c00797eebb58244dddab7fd9a348d70090c3871004c320f38d",
        ),
    ),
    "windows-vulkan": (
        _a(
            "sd-master-{c}-bin-win-vulkan-x64.zip",
            29874869,
            "a6d83fd98f716de5f0845b1b00cfbd01211f1e89a7fc2b8285516c48537c5b46",
        ),
    ),
    "windows-cpu": (
        _a(
            "sd-master-{c}-bin-win-cpu-x64.zip",
            18106019,
            "583246569b26ec1cf63dbe006b1824565708efb4d4c7cf302221ac6b69c4f2bf",
        ),
    ),
}


def select_variant(hw: HardwareInfo, *, has_vulkan: Callable[[], bool] = _has_vulkan) -> str:
    """Which build makes images here: Metal on Apple Silicon, CUDA 12 for NVIDIA on Windows,
    Vulkan when a Vulkan loader is present (NVIDIA on Linux, AMD, Intel), else CPU."""
    x64 = hw.arch.lower() in ("x86_64", "amd64")
    if hw.os == "macos":
        if hw.arch == "arm64":
            return "macos-arm64"
        raise UnsupportedPlatform("Intel Macs aren't supported yet.")
    if not x64:
        raise UnsupportedPlatform(
            f"No stable-diffusion.cpp build is pinned for {hw.os} on {hw.arch}."
        )
    if hw.os == "windows":
        nvidia = [g for g in hw.gpus if g.vendor == "nvidia" and g.cuda_driver]
        if nvidia and int(nvidia[0].cuda_driver.split(".")[0]) >= 12:  # type: ignore[union-attr]
            return "windows-cuda12"
        return "windows-vulkan" if has_vulkan() else "windows-cpu"
    return "linux-vulkan" if has_vulkan() else "linux-cpu"


class SdCppInstaller:
    """Downloads and unpacks a stable-diffusion.cpp build under
    <runners>/sd.cpp/<build>/<variant>/, the way LlamaCppInstaller does llama.cpp's."""

    MARKER = "installed.json"

    def __init__(
        self,
        runners_dir: Path,
        *,
        client_factory: Callable[[], httpx2.AsyncClient] = _default_client,
        base_url: str = RELEASE_URL,
        variants: dict[str, tuple[Asset, ...]] = VARIANTS,
    ) -> None:
        self.runners_dir = runners_dir
        self.client_factory = client_factory
        self.base_url = base_url.rstrip("/")
        self.variants = variants
        self._locks: dict[str, asyncio.Lock] = {}  # one install per variant at a time

    def install_dir(self, variant: str) -> Path:
        return self.runners_dir / "sd.cpp" / SD_CPP_BUILD / variant

    def cli_path(self, variant: str) -> Path | None:
        """The installed sd-cli, or None if this variant isn't installed."""
        marker = self.install_dir(variant) / self.MARKER
        if not marker.is_file():
            return None
        installed = json.loads(marker.read_text(encoding="utf-8"))
        cli = self.install_dir(variant) / installed["cli"]
        return cli if cli.is_file() else None

    def size(self, variant: str) -> int:
        """Bytes to download for the variant (0 once it's installed)."""
        if self.cli_path(variant) is not None:
            return 0
        return sum(a.size for a in self.variants.get(variant, ()))

    async def install(self, variant: str, on_bytes: Callable[[int], None] = lambda n: None) -> Path:
        async with self._locks.setdefault(variant, asyncio.Lock()):
            return await self._install(variant, on_bytes)  # the second finds it installed

    async def _install(self, variant: str, on_bytes: Callable[[int], None]) -> Path:
        existing = self.cli_path(variant)
        if existing is not None:
            return existing
        assets = self.variants.get(variant)
        if assets is None:
            raise UnsupportedPlatform(f"Unknown stable-diffusion.cpp variant {variant!r}.")
        root = self.install_dir(variant)
        downloads = root / "downloads"
        bin_dir = root / "bin"
        async with self.client_factory() as client:
            for asset in assets:
                await fetch_verified(
                    client,
                    f"{self.base_url}/{asset.name}",
                    downloads / asset.name,
                    size=asset.size,
                    sha256=asset.sha256,
                    on_bytes=on_bytes,
                )
        if bin_dir.exists():
            shutil.rmtree(bin_dir)
        for asset in assets:
            _unpack(downloads / asset.name, bin_dir)
        cli = _find_cli(bin_dir)
        _gather_libraries(bin_dir, cli.parent)
        if sys.platform != "win32":
            cli.chmod(cli.stat().st_mode | 0o111)
        (root / self.MARKER).write_text(
            json.dumps(
                {
                    "build": SD_CPP_BUILD,
                    "variant": variant,
                    "cli": str(cli.relative_to(root)),
                    "assets": [a.sha256 for a in assets],
                }
            ),
            encoding="utf-8",
        )
        shutil.rmtree(downloads)  # the unpacked build is what we keep
        return cli


def _find_cli(bin_dir: Path) -> Path:
    name = "sd-cli.exe" if sys.platform == "win32" else "sd-cli"
    found = sorted(bin_dir.rglob(name), key=lambda p: len(p.parts))
    if not found:
        raise DownloadError(f"The stable-diffusion.cpp build has no {name}.")
    return found[0]
