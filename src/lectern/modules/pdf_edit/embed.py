"""Completing the fonts PDFium writes into a filled form.

PDFium draws a field's value in the font its `/DA` names. For letters that font can't draw
(Cyrillic in a `/Helv` field), it adds a system font of its own choosing ("Arial" in the cp1251
layout) and doesn't embed it, so the value shows only where a viewer has that font. Naming an
embedded font in the field doesn't change its choice (docs/acceptance/pdf-edit.md, "Cyrillic in
filled fields"). So after the fill, each such font gets an installed font's file, cut down to
the letters drawn, as an incremental update: PDFium's bytes stay as they were.
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass, field

from fontTools.agl import toUnicode
from fontTools.ttLib import TTFont
from pypdf import PdfReader, PdfWriter
from pypdf.errors import PyPdfError
from pypdf.generic import ArrayObject, DictionaryObject, NameObject, NumberObject, StreamObject

from lectern.modules.pdf_edit import fonts as font_index

log = logging.getLogger(__name__)

# The fonts every viewer has; PDFium's `/Helv` is Helvetica.
STANDARD_14 = frozenset({
    "Courier", "Courier-Bold", "Courier-Oblique", "Courier-BoldOblique",
    "Helvetica", "Helvetica-Bold", "Helvetica-Oblique", "Helvetica-BoldOblique",
    "Times-Roman", "Times-Bold", "Times-Italic", "Times-BoldItalic", "Symbol", "ZapfDingbats",
})  # fmt: skip
# PDFium names a system font by its family ("Arial,Bold"); the installed file goes by its
# PostScript name.
ALIASES = {
    "Arial": "ArialMT",
    "Arial,Bold": "Arial-BoldMT",
    "Arial,Italic": "Arial-ItalicMT",
    "Arial,BoldItalic": "Arial-BoldItalicMT",
}
# When the font PDFium named isn't installed (Linux has no Arial): a sans with Cyrillic.
FALLBACK_SANS = ("LiberationSans", "DejaVuSans", "NotoSans-Regular")
FONT_FILES = ("/FontFile", "/FontFile2", "/FontFile3")
BASE_ENCODINGS = {"/WinAnsiEncoding": "cp1252", "/MacRomanEncoding": "mac_roman"}
ENCRYPTED = "the encrypted form's font"


@dataclass
class Embedded:
    data: bytes  # the PDF with its fonts completed; the bytes given when nothing changed
    missing: list[str] = field(default_factory=list)  # the fonts that stay unembedded


def embed_fonts(
    data: bytes,
    text: str,
    fonts: font_index.InstalledFonts,
    *,
    aliases: dict[str, str] | None = None,
    fallbacks: tuple[str, ...] | None = None,
) -> Embedded:
    """`data` (PDFium's filled form) with each font a widget's appearance uses that isn't
    embedded, and isn't one of the standard 14, given an installed font's file cut down to the
    letters it draws: those of `text` and of every field shown in it, the ones filled before
    included, since they share it. A font no installed file can stand in for stays as it was
    and is named in `missing`. `aliases` and `fallbacks` default to ALIASES and FALLBACK_SANS."""
    aliases = ALIASES if aliases is None else aliases
    fallbacks = FALLBACK_SANS if fallbacks is None else fallbacks
    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted:
        # pypdf writes no incremental update to an encrypted file (docs/acceptance/pdf-edit.md).
        return Embedded(data, [ENCRYPTED] if _has_unembedded(reader) else [])
    writer = PdfWriter(reader, incremental=True)
    missing: list[str] = []
    changed = False
    for number, (font, shown) in enumerate(_unembedded(writer.pages)):
        name = str(font["/BaseFont"])[1:]
        letters = _letters(font, text + shown)
        if not letters:
            continue
        found = _installed(fonts, name, aliases, fallbacks)
        if found is None:
            missing.append(name)
            continue
        ps_name, installed = found
        drawn = "".join(sorted(set(letters.values())))
        tag = font_index.subset_tag(ps_name, drawn, number)
        try:
            subset = font_index.subset_for(installed, ps_name, drawn, tag)
        except font_index.NotEmbeddable as exc:
            log.info("Left %s unembedded in a filled form: %s", name, exc)
            missing.append(name)
            continue
        _complete(writer, font, subset, f"{tag}+{ps_name}", letters)
        changed = True
    if not changed:
        return Embedded(data, missing)
    out = io.BytesIO()
    writer.write(out)
    return Embedded(out.getvalue(), missing)


def _unembedded(pages: object) -> list[tuple[DictionaryObject, str]]:
    """Each font in a widget's normal appearance that a viewer has to have (not embedded, not
    one of the standard 14), with the values of the fields drawn in it. Once each, however many
    fields share it."""
    found: dict[int, tuple[DictionaryObject, str]] = {}
    for page in pages:
        for annot in _resolved(page.get("/Annots")) or []:
            annot = annot.get_object()
            normal = _resolved((_resolved(annot.get("/AP")) or {}).get("/N"))
            if normal is None:
                continue
            streams = [normal] if isinstance(normal, StreamObject) else list(normal.values())
            for stream in streams:
                resources = _resolved(stream.get_object().get("/Resources")) or {}
                for ref in (_resolved(resources.get("/Font")) or {}).values():
                    font = ref.get_object()
                    if _needs_file(font):
                        shown = found.get(ref.idnum, (font, ""))[1]
                        found[ref.idnum] = (font, shown + _value(annot))
    return list(found.values())


def _value(annot: DictionaryObject) -> str:
    """The value a widget shows: its own `/V`, or its field's when it is one of several
    widgets; a list's chosen options joined."""
    value = annot.get("/V")
    if value is None and "/Parent" in annot:
        value = annot["/Parent"].get_object().get("/V")
    value = _resolved(value)
    if isinstance(value, list):
        return "".join(str(item) for item in value)
    return str(value) if value is not None else ""


def _resolved(value: object) -> object:
    """The object a reference points at; the value itself otherwise (None stays None)."""
    return value.get_object() if value is not None else None


def _needs_file(font: DictionaryObject) -> bool:
    if font.get("/Subtype") not in ("/TrueType", "/Type1") or "/FontDescriptor" not in font:
        return False
    if str(font.get("/BaseFont", ""))[1:] in STANDARD_14:
        return False
    descriptor = font["/FontDescriptor"].get_object()
    return not any(key in descriptor for key in FONT_FILES)


def _has_unembedded(reader: PdfReader) -> bool:
    """Whether an encrypted form has a font to complete. It can be read with no password when
    only an owner password is set (and RC4, or AES with `cryptography` installed); one that
    can't be read is taken to have one."""
    try:
        reader.decrypt("")
        return bool(_unembedded(reader.pages))
    except (PyPdfError, NotImplementedError) as exc:
        log.info("Couldn't read an encrypted filled form's fonts: %s", exc)
        return True


def _letters(font: DictionaryObject, text: str) -> dict[int, str]:
    """The font's character codes that draw a letter of `text`: its base encoding, with its
    `/Differences` read through their glyph names."""
    encoding = font.get("/Encoding")
    encoding = encoding.get_object() if encoding is not None else None
    base, differences = "cp1252", {}
    if isinstance(encoding, NameObject):
        base = BASE_ENCODINGS.get(encoding, base)
    elif isinstance(encoding, DictionaryObject):
        base = BASE_ENCODINGS.get(encoding.get("/BaseEncoding"), base)
        code = 0
        for item in encoding.get("/Differences", []):
            if isinstance(item, NameObject):
                differences[code] = toUnicode(item[1:])
                code += 1
            else:
                code = int(item)
    wanted = set(text)
    letters = {}
    for code in range(int(font.get("/FirstChar", 0)), int(font.get("/LastChar", 255)) + 1):
        letter = differences.get(code) or bytes([code]).decode(base, errors="ignore")
        if letter in wanted:
            letters[code] = letter
    return letters


def _installed(
    fonts: font_index.InstalledFonts,
    name: str,
    aliases: dict[str, str],
    fallbacks: tuple[str, ...],
) -> tuple[str, font_index.Installed] | None:
    """The font file to embed: the one PDFium named, else the first installed fallback."""
    for ps_name in (aliases.get(name, name), *fallbacks):
        found = fonts.find(ps_name)
        if found is not None:
            return ps_name, found
    return None


def _complete(
    writer: PdfWriter,
    font: DictionaryObject,
    subset: bytes,
    name: str,
    letters: dict[int, str],
) -> None:
    """`font` given `subset` as its file and name, and the widths of the letters it draws taken
    from it: the same as PDFium's for the font PDFium used, a fallback's own otherwise."""
    file = StreamObject()
    file.set_data(subset)
    file[NameObject("/Length1")] = NumberObject(len(subset))
    descriptor = font["/FontDescriptor"].get_object()
    descriptor[NameObject("/FontFile2")] = writer._add_object(file)
    descriptor[NameObject("/FontName")] = NameObject(f"/{name}")
    font[NameObject("/BaseFont")] = NameObject(f"/{name}")
    face = TTFont(io.BytesIO(subset))
    try:
        glyphs, per_em, advances = face.getBestCmap(), face["head"].unitsPerEm, face["hmtx"]
        first = int(font.get("/FirstChar", 0))
        widths = list(font.get("/Widths", []))
        for code, letter in letters.items():
            glyph = glyphs.get(ord(letter))
            if glyph is not None and 0 <= code - first < len(widths):
                widths[code - first] = NumberObject(round(advances[glyph][0] * 1000 / per_em))
        font[NameObject("/Widths")] = ArrayObject(widths)
    finally:
        face.close()
