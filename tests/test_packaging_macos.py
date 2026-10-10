"""Packaging for a Mac: the menu-bar app (packaging/macos/build_app.sh)."""

from __future__ import annotations

import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MACOS = ROOT / "packaging" / "macos"

# Swift isn't compiled anywhere else: CI is Linux. These run on a Mac with the Command Line Tools.
needs_swiftc = pytest.mark.skipif(
    sys.platform != "darwin" or shutil.which("swiftc") is None,
    reason="building the app needs macOS with the Command Line Tools (swiftc)",
)


def _build(*args: str, **env: str) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603
        ["bash", str(MACOS / "build_app.sh"), *args],  # noqa: S607
        env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", **env},
        capture_output=True, text=True, timeout=300,
    )  # fmt: skip


@needs_swiftc
def test_the_app_builds_into_a_signed_bundle(tmp_path):
    bundle = tmp_path / "Lectern.app"
    built = _build(str(bundle), "/Users/someone/.lectern", "1.2.3")
    assert built.returncode == 0, built.stderr

    assert (bundle / "Contents/MacOS/Lectern").read_bytes()[:4] == b"\xcf\xfa\xed\xfe"  # Mach-O
    info = plistlib.loads((bundle / "Contents/Info.plist").read_bytes())
    assert info["CFBundleExecutable"] == "Lectern"
    assert info["CFBundleShortVersionString"] == "1.2.3"
    assert info["LecternDir"] == "/Users/someone/.lectern"
    assert info["LSUIElement"] is True  # a menu-bar item, no Dock icon
    verified = subprocess.run(  # noqa: S603
        ["codesign", "--verify", "--deep", "--strict", str(bundle)],  # noqa: S607
        capture_output=True, text=True, timeout=60,
    )  # fmt: skip
    assert verified.returncode == 0, verified.stderr


@needs_swiftc
def test_building_again_replaces_the_bundle(tmp_path):
    bundle = tmp_path / "Lectern.app"
    assert _build(str(bundle), "/Users/someone/.lectern", "1.0.0").returncode == 0
    (bundle / "Contents/left-over").touch()
    assert _build(str(bundle), "/Users/someone/.lectern", "1.1.0").returncode == 0
    info = plistlib.loads((bundle / "Contents/Info.plist").read_bytes())
    assert info["CFBundleShortVersionString"] == "1.1.0"
    assert not (bundle / "Contents/left-over").exists()


def test_the_info_plist_template_is_a_plist_with_both_placeholders():
    text = (MACOS / "tray/Info.plist").read_text()
    info = plistlib.loads(text.encode())
    assert info["CFBundleShortVersionString"] == "@VERSION@"
    assert info["LecternDir"] == "@LECTERN_DIR@"


def test_build_app_only_writes_to_a_path_that_is_an_app(tmp_path):
    # It starts by removing what's at the path: never a folder that isn't a bundle.
    folder = tmp_path / "documents"
    folder.mkdir()
    (folder / "keep").touch()
    done = _build(str(folder), "/Users/someone/.lectern", "1.0.0")
    assert done.returncode == 1 and ".app" in done.stderr
    assert (folder / "keep").exists()


@pytest.mark.parametrize("where", ["relative/lectern", "/Users/some one/.lectern", "/Users/a<b"])
def test_build_app_refuses_an_install_folder_the_plist_cant_hold(tmp_path, where):
    done = _build(str(tmp_path / "Lectern.app"), where, "1.0.0")
    assert done.returncode == 1 and "install folder" in done.stderr
    assert not (tmp_path / "Lectern.app").exists()


def test_build_app_without_swiftc_says_how_to_get_it(tmp_path):
    done = _build(str(tmp_path / "Lectern.app"), "/Users/someone/.lectern", "1.0.0",
                  LECTERN_SWIFTC="no-such-swiftc")  # fmt: skip
    assert done.returncode == 1 and "xcode-select --install" in done.stderr
    assert not (tmp_path / "Lectern.app").exists()
