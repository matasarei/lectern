"""The fonts installed on this computer, by PostScript name, for an edit whose new letters the
PDF's own (subset) font doesn't have: the installed copy of the same font has them. Only a
TrueType-outline font whose licence allows embedding is used, and only the letters an edit
needs are embedded (fontTools subsets it; PDFium would embed the whole file).

A PDF names its subset fonts "ABCDEF+TimesNewRomanPSMT"; the part after the plus is the
PostScript name an installed font carries in its name table (ID 6).
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
import re
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

from fontTools import subset
from fontTools.ttLib import TTCollection, TTFont, TTLibError

log = logging.getLogger(__name__)
# fontTools warns about every table it doesn't know (Apple's morx, feat…) and about quirks in
# Apple's fonts it reads around; expected for a font cut down for a PDF, noise in the log.
logging.getLogger("fontTools").setLevel(logging.ERROR)

SUBSET_PREFIX = re.compile(r"^[A-Z]{6}\+")
FONT_SUFFIXES = (".ttf", ".ttc", ".otf")
# OS/2 fsType: 0x0002 "restricted licence embedding" (may not be embedded at all), 0x0200
# "bitmap embedding only". Anything else allows embedding the outlines we need.
NO_EMBEDDING = 0x0002
BITMAP_ONLY = 0x0200


def postscript_name(pdf_font_name: str) -> str:
    """'BAAAAA+TimesNewRomanPSMT' → 'TimesNewRomanPSMT'."""
    return SUBSET_PREFIX.sub("", pdf_font_name)


def default_dirs() -> list[Path]:
    """Where this platform keeps installed fonts, the user's own included."""
    home = Path.home()
    if sys.platform == "darwin":
        return [
            Path("/System/Library/Fonts"),
            Path("/Library/Fonts"),
            home / "Library" / "Fonts",
        ]
    if sys.platform == "win32":
        windir = Path(os.environ.get("WINDIR", r"C:\Windows"))
        local = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
        return [windir / "Fonts", local / "Microsoft" / "Windows" / "Fonts"]
    return [
        Path("/usr/share/fonts"),
        Path("/usr/local/share/fonts"),
        home / ".local" / "share" / "fonts",
        home / ".fonts",
    ]


@dataclass(frozen=True)
class Installed:
    path: Path
    index: int  # the face within a .ttc collection; 0 otherwise


class NotEmbeddable(Exception):
    """The installed font can't be used: its licence forbids embedding, or it has no
    TrueType outlines (PDFium embeds only those)."""


class InstalledFonts:
    """An index of the fonts under `dirs`, built once and kept for the life of the process.
    apply_edits builds it before the first edit, needed or not, so it never runs while PDFium
    is locked: 0.45 s for 904 fonts on the developer's Mac (measured 2026-11-08)."""

    def __init__(self, dirs: list[Path]) -> None:
        self._dirs = dirs
        self._lock = threading.Lock()
        self._by_name: dict[str, Installed] | None = None

    def load(self) -> dict[str, Installed]:
        """The index, built now if it isn't yet (it opens every font file)."""
        with self._lock:
            if self._by_name is None:
                self._by_name = self._scan()
            return self._by_name

    def find(self, ps_name: str) -> Installed | None:
        return self.load().get(ps_name)

    def _scan(self) -> dict[str, Installed]:
        found: dict[str, Installed] = {}
        for folder in self._dirs:
            if not folder.is_dir():
                continue
            for path in sorted(folder.rglob("*")):
                if path.suffix.lower() not in FONT_SUFFIXES or not path.is_file():
                    continue
                for index, name in _names(path):
                    found.setdefault(name, Installed(path, index))
        return found


def _names(path: Path) -> list[tuple[int, str]]:
    """(face index, PostScript name) for each face in the file; nothing if it can't be read."""
    try:
        opened = TTCollection(path, lazy=True) if path.suffix.lower() == ".ttc" else None
        single = None if opened else TTFont(path, lazy=True)
    except (TTLibError, OSError, AssertionError, ValueError):
        log.debug("Skipped a font file that couldn't be read: %s", path.name)
        return []
    try:
        faces = opened.fonts if opened else [single]
        names = []
        for index, face in enumerate(faces):
            name = face["name"].getDebugName(6) if "name" in face else None
            if name:
                names.append((index, name))
        return names
    except (TTLibError, OSError, KeyError, AssertionError, ValueError):
        log.debug("Skipped a font file that couldn't be read: %s", path.name)
        return []
    finally:
        (opened or single).close()


def subset_tag(ps_name: str, text: str, n: int) -> str:
    """Six capital letters naming one embedded subset ("KQZRTB+TimesNewRomanPSMT"), as PDF
    subsets are named. Each must be unique in a document: PDFium takes a second font loaded
    under a name it has seen for the first, and draws the wrong letters. A hash, so editing
    an edited PDF again doesn't reuse a tag that is already in it."""
    digest = hashlib.sha256(f"{ps_name}|{text}|{n}".encode()).digest()
    return "".join(chr(ord("A") + b % 26) for b in digest[:6])


def subset_for(font: Installed, ps_name: str, text: str, tag: str) -> bytes:
    """The installed font cut down to the letters of `text`, as TrueType bytes for PDFium,
    its PostScript name prefixed with `tag`. Raises NotEmbeddable when its licence or
    outlines rule it out."""
    face = TTFont(font.path, fontNumber=font.index)
    try:
        fs_type = face["OS/2"].fsType if "OS/2" in face else 0
        if fs_type & NO_EMBEDDING or fs_type & BITMAP_ONLY:
            raise NotEmbeddable("its licence doesn't allow embedding it")
        if "glyf" not in face:
            raise NotEmbeddable("it has no TrueType outlines")
        options = subset.Options()
        options.name_IDs = ["*"]  # keep its names: the PDF shows the font's real name
        options.notdef_outline = True
        options.layout_features = []  # no ligatures or kerning tables: PDFium doesn't use them
        subsetter = subset.Subsetter(options)
        subsetter.populate(text=text)
        subsetter.subset(face)
        # The PostScript name in both the Mac and the Windows records: PDFium reads the
        # Windows one, and Apple's own fonts often have only the Mac one ("Untitled" then).
        name = f"{tag}+{ps_name}"
        face["name"].setName(name, 6, 3, 1, 0x409)
        face["name"].setName(name, 6, 1, 0, 0)
        out = io.BytesIO()
        face.save(out)
        return out.getvalue()
    finally:
        face.close()


_default: InstalledFonts | None = None
_default_lock = threading.Lock()


def installed() -> InstalledFonts:
    """This computer's fonts (one index per process)."""
    global _default
    with _default_lock:
        if _default is None:
            _default = InstalledFonts(default_dirs())
        return _default
