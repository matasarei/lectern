"""PDFs the `pdf` agent-check set works on, made at run time like its lease DOCX, and what a
check reads back from the PDFs the chat saved.

The agreement is drawn in an installed font cut to the letters it uses, the way most PDFs carry
their fonts, so an edit that needs a new letter goes through the same path as a real PDF's
(the installed copy of the font). The form is written by hand: two text fields, a checkbox, a
radio group, a list, a read-only field and a button. All names are made up.
"""

from __future__ import annotations

import ctypes
from pathlib import Path

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c

from lectern.modules.pdf_edit import fonts as font_index
from lectern.modules.pdf_edit.forms import form_fields
from lectern.projects.text import PDFIUM_LOCK

# Fonts with Latin and Cyrillic that one of macOS, Windows or a Linux desktop has.
FONT_CANDIDATES = ("ArialMT", "TimesNewRomanPSMT", "LiberationSans", "DejaVuSans", "Arimo")

# (text, x, size) runs per line; x None follows the run before.
AGREEMENT: list[list[tuple[str, float | None, float]]] = [
    [("Service Agreement No. 47/2026", 72, 18)],
    [("This agreement is made on 12 March 2026 between Acme Translation Ltd. (the", 72, 11)],
    [("Contractor) and Maria Kovalenko (the Client).", 72, 11)],
    [("The Contractor shall translate the documents listed below from Ukrainian into", 72, 11)],
    [("English within 14 calendar days of receiving them. The total fee is EUR 1,250.00,", 72, 11)],
    [("payable in two equal instalments.", 72, 11)],
    [("Договір укладено у місті Київ. Сторони погодили, що оплата здійснюється", 72, 11)],
    [("на рахунок Виконавця протягом п’яти банківських днів.", 72, 11)],
    [("Item", 72, 11), ("Pages", 260, 11), ("Price", 330, 11)],
    [("Birth certificate", 72, 11), ("2", 260, 11), ("EUR 150.00", 330, 11)],
    [("Diploma with transcript", 72, 11), ("9", 260, 11), ("EUR 1,100.00", 330, 11)],
    [("Contact: office@acme-translation.example, phone +380 44 123 4567.", 72, 9)],
    [("Signed: ____________ Olena Shevchenko, Director", 72, 11)],
]


def write_agreement(path: Path) -> Path:
    fonts = font_index.installed()
    found = next(
        ((name, hit) for name in FONT_CANDIDATES if (hit := fonts.find(name)) is not None), None
    )
    if found is None:
        raise ValueError(
            "The pdf set needs one of these fonts installed: " + ", ".join(FONT_CANDIDATES)
        )
    ps_name, installed = found
    text = "".join(run for line in AGREEMENT for run, _, _ in line)
    data = font_index.subset_for(installed, ps_name, text, "AAAAAA")
    keep: list[object] = []
    with PDFIUM_LOCK:
        pdf = pdfium.PdfDocument.new()
        try:
            buffer = (ctypes.c_uint8 * len(data)).from_buffer_copy(data)
            keep.append(buffer)
            font = pdfium_c.FPDFText_LoadFont(
                pdf.raw, buffer, len(data), pdfium_c.FPDF_FONT_TRUETYPE, 1
            )
            page = pdf.new_page(612, 792)
            y = 720.0
            for line in AGREEMENT:
                for run, x, size in line:
                    obj = pdfium_c.FPDFPageObj_CreateTextObj(pdf.raw, font, size)
                    encoded = (run + "\0").encode("utf-16-le")
                    text_buffer = ctypes.create_string_buffer(encoded, len(encoded))
                    keep.append(text_buffer)
                    pdfium_c.FPDFText_SetText(
                        obj, ctypes.cast(text_buffer, pdfium_c.FPDF_WIDESTRING)
                    )
                    pdfium_c.FPDFPageObj_Transform(obj, 1, 0, 0, 1, x, y)
                    pdfium_c.FPDFPage_InsertObject(page.raw, obj)
                y -= 26 if line[0][2] > 12 else 17
            pdfium_c.FPDFPage_GenerateContent(page.raw)
            pdfium_c.FPDFFont_Close(font)
            page.close()
            path.parent.mkdir(parents=True, exist_ok=True)
            pdf.save(path)
        finally:
            pdf.close()
    return path


def write_form(path: Path) -> Path:
    """A one-page form: text fields "name" and "date", a checkbox "agree", a radio group
    "size" (S, M), a list "city", a read-only "id" and a push button "print"."""
    on = b"0 g 2 2 10 10 re f"
    widget = b"/Type /Annot /Subtype /Widget /F 4 /P 3 0 R "
    content = b"BT /F1 12 Tf 72 720 Td (Full name:) Tj 0 -40 Td (Date:) Tj ET\n"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R /AcroForm 5 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 9 0 R >> >> "
        b"/Annots [6 0 R 7 0 R 8 0 R 12 0 R 13 0 R 14 0 R 15 0 R 16 0 R] >>",
        b"<< /Length %d >>\nstream\n" % len(content) + content + b"endstream",
        b"<< /Fields [6 0 R 7 0 R 8 0 R 11 0 R 14 0 R 15 0 R 16 0 R] "
        b"/DR << /Font << /Helv 9 0 R >> >> /DA (/Helv 0 Tf 0 g) >>",
        b"<< " + widget + b"/FT /Tx /T (name) /Rect [180 710 450 730] "
        b"/DA (/Helv 11 Tf 0 g) /V () >>",
        b"<< " + widget + b"/FT /Tx /T (date) /Rect [180 670 300 690] "
        b"/DA (/Helv 11 Tf 0 g) /V () >>",
        b"<< " + widget + b"/FT /Btn /T (agree) /Rect [180 632 194 646] /V /Off /AS /Off "
        b"/MK << >> /AP << /N << /Yes 10 0 R /Off 17 0 R >> >> >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
        b"<< /Type /XObject /Subtype /Form /BBox [0 0 14 14] /Length %d >>\nstream\n" % len(on)
        + on
        + b"\nendstream",
        b"<< /FT /Btn /Ff 49152 /T (size) /V /Off /Kids [12 0 R 13 0 R] >>",
        b"<< " + widget + b"/Parent 11 0 R /Rect [180 592 194 606] /AS /Off /MK << >> "
        b"/AP << /N << /S 10 0 R /Off 17 0 R >> >> >>",
        b"<< " + widget + b"/Parent 11 0 R /Rect [220 592 234 606] /AS /Off /MK << >> "
        b"/AP << /N << /M 10 0 R /Off 17 0 R >> >> >>",
        b"<< " + widget + b"/FT /Ch /Ff 131072 /T (city) /Opt [(Kyiv) (Lviv) (Odesa)] /V () "
        b"/Rect [180 552 300 572] /DA (/Helv 11 Tf 0 g) >>",
        b"<< " + widget + b"/FT /Tx /Ff 1 /T (id) /V (A-17) /Rect [180 512 300 532] "
        b"/DA (/Helv 11 Tf 0 g) >>",
        b"<< " + widget + b"/FT /Btn /Ff 65536 /T (print) /Rect [180 472 260 492] >>",
        b"<< /Type /XObject /Subtype /Form /BBox [0 0 14 14] /Length 0 >>\nstream\n\nendstream",
    ]
    out = bytearray(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(out))
    return path


def pdf_text(path: Path) -> str:
    """Every page's text, its whitespace made single spaces."""
    with PDFIUM_LOCK:
        pdf = pdfium.PdfDocument(path)
        try:
            parts = []
            for index in range(len(pdf)):
                page = pdf[index]
                textpage = page.get_textpage()
                parts.append(textpage.get_text_range())
                textpage.close()
                page.close()
        finally:
            pdf.close()
    return " ".join(" ".join(parts).split())


def form_values(path: Path) -> dict[str, str]:
    return {f.name: f.value for f in form_fields(path)}
