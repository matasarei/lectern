"""Make the Windows app's icon from the README's: docs/lectern-icon.png, drawn by the Mac app
(packaging/macos/tray/Icon.swift). Run again after that changes:

    uv run python packaging/windows/tray/make_icon.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[3]
SIZES = [(16, 16), (20, 20), (24, 24), (32, 32), (40, 40), (48, 48), (64, 64), (256, 256)]


def main() -> None:
    with Image.open(ROOT / "docs/lectern-icon.png") as source:
        source.convert("RGBA").save(Path(__file__).with_name("lectern.ico"), sizes=SIZES)


if __name__ == "__main__":
    main()
