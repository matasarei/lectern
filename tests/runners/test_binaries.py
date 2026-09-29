from __future__ import annotations

import asyncio
import hashlib
import io
import json
import sys
import tarfile
import zipfile

import httpx2
import pytest

from lectern.downloads.fetch import DownloadError
from lectern.hardware.probe import GIB, Gpu, HardwareInfo
from lectern.runners import binaries
from lectern.runners.binaries import (
    VARIANTS,
    Asset,
    LlamaCppInstaller,
    UnsupportedPlatform,
    select_variant,
)


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
        "nvidia",
        "card",
        8 * GIB,
        8 * GIB,
        unified=False,
        cuda_driver=cuda,
        compute_capability=capability,
    )


@pytest.mark.parametrize(
    ("hw", "vulkan", "expected"),
    [
        (_hw(gpus=[_nvidia("13.0")]), False, "linux-cuda13"),
        (_hw(gpus=[_nvidia("12.4")]), False, "linux-cuda12"),
        (_hw(gpus=[_nvidia("11.8")]), True, "linux-vulkan"),  # too old for the CUDA builds
        # GTX 1050 Ti (Pascal, 6.1) on a CUDA 13 driver, as on archive.local: the CUDA 13
        # build has no Pascal code, so it gets the CUDA 12 build.
        (_hw(gpus=[_nvidia("13.0", "6.1")]), False, "linux-cuda12"),
        (_hw(gpus=[_nvidia("13.0", "7.0")]), False, "linux-cuda12"),  # Volta
        (_hw(gpus=[_nvidia("13.0", "7.5")]), False, "linux-cuda13"),  # Turing, e.g. GTX 1650
        (_hw(gpus=[_nvidia("13.0", None)]), False, "linux-cuda12"),  # unknown: widest build
        (_hw(), True, "linux-vulkan"),
        (_hw(), False, "linux-cpu"),
        (
            _hw("macos", "arm64", [Gpu("apple", "M4", GIB, None, unified=True)]),
            False,
            "macos-arm64",
        ),
        (_hw("windows", "AMD64", [_nvidia("13.0")]), False, "windows-cuda13"),
        (_hw("windows", "AMD64", [_nvidia("12.8")]), False, "windows-cuda12"),
        (_hw("windows", "AMD64", [_nvidia("13.0", "6.1")]), False, "windows-cuda12"),
        (_hw("windows", "AMD64"), True, "windows-vulkan"),
        (_hw("windows", "AMD64"), False, "windows-cpu"),
    ],
)
def test_select_variant(hw, vulkan, expected):
    assert select_variant(hw, has_vulkan=lambda: vulkan) == expected
    assert expected in VARIANTS


@pytest.mark.parametrize(("os_name", "arch"), [("macos", "x86_64"), ("linux", "aarch64")])
def test_unpinned_platforms_are_refused(os_name, arch):
    with pytest.raises(UnsupportedPlatform):
        select_variant(_hw(os_name, arch), has_vulkan=lambda: False)


def test_every_pinned_asset_has_size_and_sha256():
    for assets in VARIANTS.values():
        for asset in assets:
            assert asset.size > 0
            assert len(asset.sha256) == 64
            assert binaries.LLAMA_CPP_BUILD in asset.name or asset.name.startswith("cudart-")


# --- installing ----------------------------------------------------------------------------

SERVER = "llama-server.exe" if sys.platform == "win32" else "llama-server"


def _tar(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o644
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


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
    installer = LlamaCppInstaller(
        tmp_path / "runners",
        client_factory=lambda: httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
        base_url="https://example.test/rel",
        variants={variant: assets},
    )
    return installer, requests


def test_install_unpacks_and_gathers_cuda_libraries(tmp_path):
    main = _tar({f"llama-b1/{SERVER}": b"#!/bin/sh\necho ok\n", "llama-b1/libllama.so": b"L"})
    cudart = _tar({"cuda/lib64/libcudart.so.13": b"C"})
    installer, requests = _installer(tmp_path, {"main.tar.gz": main, "cudart.tar.gz": cudart})

    server = asyncio.run(installer.install("test"))

    assert server.name == SERVER and server.read_bytes().startswith(b"#!/bin/sh")
    assert (server.parent / "libcudart.so.13").read_bytes() == b"C"
    assert installer.server_path("test") == server
    assert not (installer.install_dir("test") / "downloads").exists()
    marker = json.loads((installer.install_dir("test") / "installed.json").read_text())
    assert marker["variant"] == "test"
    assert [str(r.url) for r in requests][0] == "https://example.test/rel/main.tar.gz"
    if sys.platform != "win32":
        assert server.stat().st_mode & 0o111


def test_second_install_downloads_nothing(tmp_path):
    installer, requests = _installer(tmp_path, {"main.tar.gz": _tar({f"b/{SERVER}": b"x"})})
    asyncio.run(installer.install("test"))
    count = len(requests)

    asyncio.run(installer.install("test"))

    assert len(requests) == count


def test_zip_builds_are_unpacked(tmp_path):
    installer, _ = _installer(tmp_path, {"main.zip": _zip({f"b/{SERVER}": b"x"})})
    assert asyncio.run(installer.install("test")).read_bytes() == b"x"


def test_tar_escaping_the_folder_is_refused(tmp_path):
    installer, _ = _installer(tmp_path, {"evil.tar.gz": _tar({"../../escape": b"x"})})
    with pytest.raises(DownloadError, match="unsafe"):
        asyncio.run(installer.install("test"))
    assert not (tmp_path / "escape").exists()


def test_zip_escaping_the_folder_is_refused(tmp_path):
    installer, _ = _installer(tmp_path, {"evil.zip": _zip({"../../escape": b"x"})})
    with pytest.raises(DownloadError, match="unsafe"):
        asyncio.run(installer.install("test"))


def test_build_without_llama_server_is_an_error(tmp_path):
    installer, _ = _installer(tmp_path, {"main.tar.gz": _tar({"b/llama-cli": b"x"})})
    with pytest.raises(DownloadError, match="llama-server"):
        asyncio.run(installer.install("test"))
    assert installer.server_path("test") is None


def test_unknown_variant_is_refused(tmp_path):
    installer, _ = _installer(tmp_path, {"main.tar.gz": b""})
    with pytest.raises(UnsupportedPlatform):
        asyncio.run(installer.install("nope"))
