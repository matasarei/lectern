"""llama.cpp builds: pick the right one for this machine, download it once, unpack it safely.

The release is pinned. Each asset's size and sha256 come from the GitHub release data for
that tag, so installing needs no GitHub API call, only the download itself.
"""

from __future__ import annotations

import ctypes.util
import json
import shutil
import sys
import tarfile
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx2

from lectern.downloads.fetch import DownloadError, fetch_verified
from lectern.hardware.probe import HardwareInfo

LLAMA_CPP_BUILD = "b11236"
RELEASE_URL = f"https://github.com/ggml-org/llama.cpp/releases/download/{LLAMA_CPP_BUILD}"


@dataclass(frozen=True)
class Asset:
    name: str
    size: int
    sha256: str


def _a(name: str, size: int, sha256: str) -> Asset:
    return Asset(name.format(b=LLAMA_CPP_BUILD), size, sha256)


# Sizes and sha256 from `gh api repos/ggml-org/llama.cpp/releases/tags/b11236` (2026-09-29).
VARIANTS: dict[str, tuple[Asset, ...]] = {
    "macos-arm64": (
        _a(
            "llama-{b}-bin-macos-arm64.tar.gz",
            11761043,
            "08761eb0d2491ac5320bfd677ea01fa9c39878e61ff44289750be11245126b8f",
        ),
    ),
    "linux-cuda13": (
        _a(
            "llama-{b}-bin-ubuntu-cuda-13.4-x64.tar.gz",
            152868922,
            "b5d7c8a2fd464b7492223aca1dc07d3a82189865deac6311f2b017e52fd43cdf",
        ),
        _a(
            "cudart-llama-{b}-bin-ubuntu-cuda-13.4-x64.tar.gz",
            440236534,
            "0d44cdfa995314667c50d48c40a36ff06b4bdf74e359eaa31227411bb72b6a17",
        ),
    ),
    "linux-cuda12": (
        _a(
            "llama-{b}-bin-ubuntu-cuda-12.8-x64.tar.gz",
            172237886,
            "661d65d36e6815b7ee23aa9e147441d66a7b20a4ba5594e374244294a5562bff",
        ),
        _a(
            "cudart-llama-{b}-bin-ubuntu-cuda-12.8-x64.tar.gz",
            594377263,
            "e4230992c6715d7a028fb10f737e9b7b716053b8f6eea19c9b75dc41e44435bc",
        ),
    ),
    "linux-vulkan": (
        _a(
            "llama-{b}-bin-ubuntu-vulkan-x64.tar.gz",
            31351800,
            "59f7e88acd7ffcdd3eb8536750da1c9f9ce2a3b8d07167f1649c936894b8b6ec",
        ),
    ),
    "linux-cpu": (
        _a(
            "llama-{b}-bin-ubuntu-x64.tar.gz",
            17406283,
            "e8228e59db26a01b0d4c05970f701040d04545cfa1e4ed63efa80c7a707a7ce1",
        ),
    ),
    "windows-cuda13": (
        _a(
            "llama-{b}-bin-win-cuda-13.4-x64.zip",
            153540960,
            "472e5fe0317bc61743f160992aa9a38b57cf52a318b3c30b7872f586fa5cd1f1",
        ),
        _a(
            "cudart-llama-bin-win-cuda-13.4-x64.zip",
            423535356,
            "738f8c251ac22b70c3ae6f83a10cf222725df0395246a2cf58f32bdb85fbe668",
        ),
    ),
    "windows-cuda12": (
        _a(
            "llama-{b}-bin-win-cuda-12.4-x64.zip",
            264523826,
            "978a78be020697e468b5608a6b42b8ecc4d25676f4fbf3c5b3df2e9880a04e19",
        ),
        _a(
            "cudart-llama-bin-win-cuda-12.4-x64.zip",
            391443627,
            "8c79a9b226de4b3cacfd1f83d24f962d0773be79f1e7b75c6af4ded7e32ae1d6",
        ),
    ),
    "windows-vulkan": (
        _a(
            "llama-{b}-bin-win-vulkan-x64.zip",
            33064176,
            "8e9f9ad6acda2ad2ad895ac91d876f86ab11123de906d74bbc1a35d13d8a3bd3",
        ),
    ),
    "windows-cpu": (
        _a(
            "llama-{b}-bin-win-cpu-x64.zip",
            19160483,
            "baa39edf51e0805e6bb53edb1d4abf40c88730b89e907edcd0d4e6dd264f51d4",
        ),
    ),
}


CUDA13_MIN_CAPABILITY = (7, 5)


def _capability(value: str | None) -> tuple[int, int]:
    """ "6.1" -> (6, 1). Unknown counts as old, so the build that covers more GPUs is chosen."""
    try:
        major, minor = (value or "").split(".")
        return int(major), int(minor)
    except ValueError:
        return (0, 0)


class UnsupportedPlatform(Exception):
    pass


def _has_vulkan() -> bool:
    return (
        ctypes.util.find_library("vulkan") is not None
        or ctypes.util.find_library("vulkan-1") is not None
    )


def select_variant(hw: HardwareInfo, *, has_vulkan: Callable[[], bool] = _has_vulkan) -> str:
    """Which build this machine runs: CUDA for NVIDIA (13.x when the driver supports it),
    Metal on Apple Silicon, Vulkan when a Vulkan loader is present (AMD, Intel), else CPU."""
    x64 = hw.arch.lower() in ("x86_64", "amd64")
    if hw.os == "macos":
        if hw.arch == "arm64":
            return "macos-arm64"
        raise UnsupportedPlatform("Intel Macs aren't supported yet.")
    if not x64:
        raise UnsupportedPlatform(f"No llama.cpp build is pinned for {hw.os} on {hw.arch}.")
    prefix = "windows" if hw.os == "windows" else "linux"
    nvidia = [g for g in hw.gpus if g.vendor == "nvidia" and g.cuda_driver]
    if nvidia:
        gpu = nvidia[0]
        driver = int(gpu.cuda_driver.split(".")[0])  # type: ignore[union-attr]
        # llama.cpp's CUDA 13 builds only carry code for compute capability 7.5 (Turing) and
        # newer; Maxwell, Pascal and Volta are built into the CUDA 12 builds only
        # (ggml/src/ggml-cuda/CMakeLists.txt @ b11236). A driver that supports CUDA 13 still
        # runs the CUDA 12 build.
        if driver >= 13 and _capability(gpu.compute_capability) >= CUDA13_MIN_CAPABILITY:
            return f"{prefix}-cuda13"
        if driver >= 12:
            return f"{prefix}-cuda12"
    return f"{prefix}-vulkan" if has_vulkan() else f"{prefix}-cpu"


def _default_client() -> httpx2.AsyncClient:
    return httpx2.AsyncClient(follow_redirects=True, timeout=httpx2.Timeout(30.0, read=120.0))


class LlamaCppInstaller:
    """Downloads and unpacks a llama.cpp build under <runners>/llama.cpp/<build>/<variant>/."""

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

    def install_dir(self, variant: str) -> Path:
        return self.runners_dir / "llama.cpp" / LLAMA_CPP_BUILD / variant

    def server_path(self, variant: str) -> Path | None:
        """The installed llama-server, or None if this variant isn't installed."""
        marker = self.install_dir(variant) / self.MARKER
        if not marker.is_file():
            return None
        server = self.install_dir(variant) / json.loads(marker.read_text())["server"]
        return server if server.is_file() else None

    def size(self, variant: str) -> int:
        """Bytes to download for the variant (0 once it's installed)."""
        if self.server_path(variant) is not None:
            return 0
        return sum(a.size for a in self.variants.get(variant, ()))

    async def install(self, variant: str, on_bytes: Callable[[int], None] = lambda n: None) -> Path:
        existing = self.server_path(variant)
        if existing is not None:
            return existing
        assets = self.variants.get(variant)
        if assets is None:
            raise UnsupportedPlatform(f"Unknown llama.cpp variant {variant!r}.")
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
        server = _find_server(bin_dir)
        _gather_libraries(bin_dir, server.parent)
        if sys.platform != "win32":
            server.chmod(server.stat().st_mode | 0o111)
        (root / self.MARKER).write_text(
            json.dumps(
                {
                    "build": LLAMA_CPP_BUILD,
                    "variant": variant,
                    "server": str(server.relative_to(root)),
                    "assets": [a.sha256 for a in assets],
                }
            )
        )
        shutil.rmtree(downloads)  # the unpacked build is what we keep
        return server


def _unpack(archive: Path, dest: Path) -> None:
    """Unpack an archive into `dest`, refusing any member that would land outside it."""
    dest.mkdir(parents=True, exist_ok=True)
    if archive.name.endswith(".zip"):
        root = dest.resolve()
        with zipfile.ZipFile(archive) as zf:
            for member in zf.infolist():
                target = (dest / member.filename).resolve()
                if not target.is_relative_to(root):
                    raise DownloadError(f"{archive.name}: unsafe path {member.filename!r}")
            zf.extractall(dest)  # noqa: S202 (every member checked above)
    else:
        with tarfile.open(archive) as tf:
            try:
                tf.extractall(dest, filter="data")  # refuses absolute paths, .., device files
            except tarfile.FilterError as exc:
                raise DownloadError(f"{archive.name}: unsafe archive member: {exc}") from exc


def _find_server(bin_dir: Path) -> Path:
    name = "llama-server.exe" if sys.platform == "win32" else "llama-server"
    found = sorted(bin_dir.rglob(name), key=lambda p: len(p.parts))
    if not found:
        raise DownloadError(f"The llama.cpp build has no {name}.")
    return found[0]


def _gather_libraries(bin_dir: Path, server_dir: Path) -> None:
    """The CUDA runtime archive may use its own folder layout; its libraries must sit next to
    llama-server so the loader finds them."""
    patterns = ("*.so", "*.so.*", "*.dll", "*.dylib")
    for pattern in patterns:
        for lib in bin_dir.rglob(pattern):
            target = server_dir / lib.name
            if lib.parent != server_dir and not target.exists():
                shutil.move(str(lib), target)
