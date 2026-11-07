from __future__ import annotations

import asyncio
import hashlib
import io
import json
import sys
import zipfile

import httpx2
import pytest

from lectern.downloads.fetch import DownloadError
from lectern.hardware.probe import GIB, Gpu, HardwareInfo
from lectern.runners import sdcpp
from lectern.runners.binaries import Asset, UnsupportedPlatform
from lectern.runners.sdcpp import VARIANTS, SdCppInstaller, select_variant


def _hw(os_name="linux", arch="x86_64", gpus=()):
    return HardwareInfo(
        os=os_name,
        arch=arch,
        cpu="c",
        cpu_cores=8,
        ram_total=16 * GIB,
        ram_available=8 * GIB,
        gpus=list(gpus),
        gpu_budget=0,
    )


def _nvidia(cuda, capability="8.9"):
    return Gpu(
        "nvidia", "card", 8 * GIB, 8 * GIB, unified=False, cuda_driver=cuda,
        compute_capability=capability,
    )  # fmt: skip


@pytest.mark.parametrize(
    ("hw", "vulkan", "expected"),
    [
        (_hw("macos", "arm64"), False, "macos-arm64"),
        (_hw("windows", "AMD64", [_nvidia("13.0")]), False, "windows-cuda12"),
        (_hw("windows", "AMD64", [_nvidia("12.4", "6.1")]), False, "windows-cuda12"),
        (_hw("windows", "AMD64", [_nvidia("11.8")]), True, "windows-vulkan"),
        (_hw("windows", "AMD64"), True, "windows-vulkan"),
        (_hw("windows", "AMD64"), False, "windows-cpu"),
        # There is no Linux CUDA build: an NVIDIA card on Linux (archive's GTX 1050 Ti) runs
        # the Vulkan one, and the CPU one when no Vulkan loader is installed.
        (_hw(gpus=[_nvidia("13.0", "6.1")]), True, "linux-vulkan"),
        (_hw(gpus=[_nvidia("13.0", "6.1")]), False, "linux-cpu"),
        (_hw(), True, "linux-vulkan"),
        (_hw(), False, "linux-cpu"),
    ],
)
def test_select_variant(hw, vulkan, expected):
    assert select_variant(hw, has_vulkan=lambda: vulkan, macos_version=lambda: (26, 1)) == expected
    assert expected in VARIANTS


@pytest.mark.parametrize(("os_name", "arch"), [("macos", "x86_64"), ("linux", "aarch64")])
def test_unpinned_platforms_are_refused(os_name, arch):
    with pytest.raises(UnsupportedPlatform):
        select_variant(_hw(os_name, arch), has_vulkan=lambda: True)


def test_every_pinned_asset_has_size_and_sha256_of_the_pinned_build():
    commit = sdcpp.SD_CPP_BUILD.rsplit("-", 1)[1]
    for assets in VARIANTS.values():
        for asset in assets:
            assert asset.size > 0 and len(asset.sha256) == 64
            assert commit in asset.name or asset.name.startswith("cudart-")
    assert sdcpp.SD_CPP_BUILD in sdcpp.RELEASE_URL


def test_a_mac_older_than_the_builds_minimum_is_refused_with_why():
    with pytest.raises(UnsupportedPlatform, match="needs macOS 26 or later"):
        select_variant(_hw("macos", "arm64"), macos_version=lambda: (15, 6))
    assert select_variant(_hw("macos", "arm64"), macos_version=lambda: (26, 0)) == "macos-arm64"


# --- installing ----------------------------------------------------------------------------

CLI = "sd-cli.exe" if sys.platform == "win32" else "sd-cli"


def _zip(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _installer(tmp_path, files: dict[str, bytes], variant="test"):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx2.Response(200, content=files[request.url.path.rsplit("/", 1)[-1]])

    assets = tuple(Asset(n, len(d), hashlib.sha256(d).hexdigest()) for n, d in files.items())
    installer = SdCppInstaller(
        tmp_path / "runners",
        client_factory=lambda: httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
        base_url="https://example.test/rel",
        variants={variant: assets},
    )
    return installer, requests


def test_install_unpacks_the_cli_and_gathers_the_cuda_runtime(tmp_path):
    main = _zip({CLI: b"#!/bin/sh\necho ok\n", "libstable-diffusion.dylib": b"L"})
    cudart = _zip({"cuda/bin/cudart64_12.dll": b"C"})
    installer, requests = _installer(tmp_path, {"main.zip": main, "cudart.zip": cudart})

    cli = asyncio.run(installer.install("test"))

    assert cli.name == CLI and cli.read_bytes().startswith(b"#!/bin/sh")
    assert (cli.parent / "cudart64_12.dll").read_bytes() == b"C"
    assert installer.cli_path("test") == cli
    assert installer.install_dir("test").parts[-3:] == ("sd.cpp", sdcpp.SD_CPP_BUILD, "test")
    assert not (installer.install_dir("test") / "downloads").exists()
    marker = json.loads((installer.install_dir("test") / "installed.json").read_text())
    assert marker["build"] == sdcpp.SD_CPP_BUILD and marker["variant"] == "test"
    assert str(requests[0].url) == "https://example.test/rel/main.zip"
    if sys.platform != "win32":
        assert cli.stat().st_mode & 0o111


def test_a_second_install_downloads_nothing_and_leaves_nothing_to_download(tmp_path):
    installer, requests = _installer(tmp_path, {"main.zip": _zip({CLI: b"x"})})
    assert installer.size("test") > 0
    asyncio.run(installer.install("test"))
    count = len(requests)

    asyncio.run(installer.install("test"))

    assert len(requests) == count and installer.size("test") == 0


def test_two_installs_at_once_download_the_build_once(tmp_path):
    installer, requests = _installer(tmp_path, {"main.zip": _zip({CLI: b"x"})})

    async def both():
        return await asyncio.gather(installer.install("test"), installer.install("test"))

    first, second = asyncio.run(both())
    assert first == second and len(requests) == 1


def test_a_build_without_sd_cli_is_an_error(tmp_path):
    installer, _ = _installer(tmp_path, {"main.zip": _zip({"sd-server": b"x"})})
    with pytest.raises(DownloadError, match="has no sd-cli"):
        asyncio.run(installer.install("test"))
    assert installer.cli_path("test") is None


def test_a_zip_escaping_the_folder_is_refused(tmp_path):
    installer, _ = _installer(tmp_path, {"main.zip": _zip({"../evil": b"x", CLI: b"x"})})
    with pytest.raises(DownloadError, match="unsafe path"):
        asyncio.run(installer.install("test"))


def test_a_corrupted_download_is_refused(tmp_path):
    good = _zip({CLI: b"x"})
    installer, _ = _installer(tmp_path, {"main.zip": good})
    installer.variants = {"test": (Asset("main.zip", len(good), "0" * 64),)}
    with pytest.raises(DownloadError):
        asyncio.run(installer.install("test"))
    assert installer.cli_path("test") is None


def test_an_unknown_variant_is_refused(tmp_path):
    installer, _ = _installer(tmp_path, {})
    with pytest.raises(UnsupportedPlatform):
        asyncio.run(installer.install("nope"))
