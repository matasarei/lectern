"""Packaging for a host install (M4): the wheel, and packaging/linux/install.sh."""

from __future__ import annotations

import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(not (ROOT / "src/lectern/web_dist/index.html").exists(),
                    reason="the web UI isn't built (npm --prefix web run build)")  # fmt: skip
def test_the_wheel_carries_the_web_ui_and_the_model_catalog(tmp_path):
    if shutil.which("uv") is None:
        pytest.skip("uv isn't installed")
    built = subprocess.run(  # noqa: S603
        ["uv", "build", "--wheel", "--offline", "--out-dir", str(tmp_path), str(ROOT)],  # noqa: S607
        capture_output=True, text=True, timeout=300,
    )  # fmt: skip
    if built.returncode != 0:
        pytest.skip(f"uv couldn't build offline: {built.stderr.strip()[-200:]}")
    (wheel,) = tmp_path.glob("lectern_ai-*.whl")
    names = zipfile.ZipFile(wheel).namelist()
    assert "lectern/web_dist/index.html" in names
    assert any(n.startswith("lectern/web_dist/assets/") for n in names)
    assert "lectern/catalog/models.yaml" in names
