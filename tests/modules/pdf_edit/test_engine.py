"""The PDF-editing engine on PDFs built here (conftest.py): fonts kept, letters a subset lacks,
the rest of the line, refusals, and how `find` is matched."""

from __future__ import annotations

import ctypes
from pathlib import Path

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c
import pytest

from lectern.modules.pdf_edit.engine import Edit, EditFailed, apply_edits
from lectern.modules.pdf_edit.fonts import InstalledFonts

from .conftest import Run, build_font, make_pdf, page_text, text_objects, width_pt

CONTRACT = [
    [
        Run("Made on 12 March 2026 by "),
        Run("Acme T", bold=True),
        Run("rading", bold=True),
        Run(" (the"),
    ],
    [Run("Contractor) and Maria Kovalenko (the Client).")],
    [Run("The total fee is EUR 1,250.00, payable in two")],
    [Run("equal instalments.")],
    [Run("Договір укладено у місті Київ.")],
    [Run("Birth certificate"), Run("EUR 150.00", x=300)],
]


@pytest.fixture
def contract(tmp_path: Path, font_files: Path) -> Path:
    return make_pdf(tmp_path / "contract.pdf", font_files, [CONTRACT])


def boxes(data: bytes, needle: str, index: int = 0) -> list[tuple[float, float]]:
    """(left, right) of each letter of `needle` where it first is on the page."""
    pdf = pdfium.PdfDocument(data)
    try:
        page = pdf[index]
        textpage = page.get_textpage()
        at = textpage.get_text_range().find(needle)
        assert at >= 0, f"{needle!r} isn't on the page"
        found = []
        left, right, bottom, top = (ctypes.c_double() for _ in range(4))
        for i in range(at, at + len(needle)):
            pdfium_c.FPDFText_GetCharBox(
                textpage.raw, i, *(ctypes.byref(v) for v in (left, right, bottom, top))
            )
            found.append((left.value, right.value))
        textpage.close()
        page.close()
        return found
    finally:
        pdf.close()


def ink_gap(data: bytes, before: str, after: str) -> float:
    """The space between the last letter of `before` and the first of `after`."""
    return boxes(data, after)[0][0] - boxes(data, before)[-1][1]


# --- What an edit keeps ----------------------------------------------------------------------


def test_an_edit_in_letters_the_font_has_keeps_the_pdfs_own_font(
    contract: Path, no_fonts: InstalledFonts
) -> None:
    result = apply_edits(contract, [Edit("Maria Kovalenko", "Mario Kovalenko")], fonts=no_fonts)

    [change] = result.changes
    assert change.after == "Contractor) and Mario Kovalenko (the Client)."
    assert change.font == "TestSans" and change.notes == [] and change.degraded == []
    assert "Contractor) and Mario Kovalenko (the Client)." in page_text(result.data)
    fonts = {font for text, font, *_ in text_objects(result.data) if "Mario" in text}
    assert fonts == {"AAAAAA+TestSans"}  # the PDF's own subset, nothing added


def test_letters_the_subset_lacks_come_from_the_installed_font_of_the_same_name(
    contract: Path, with_fonts: InstalledFonts
) -> None:
    result = apply_edits(contract, [Edit("12 March 2026", "5 April 2026")], fonts=with_fonts)

    [change] = result.changes
    assert change.font == "TestSans"
    assert change.notes == ["“A” drawn with the installed TestSans"]
    assert "Made on 5 April 2026 by Acme Trading (the" in page_text(result.data)
    [(text, font, *_)] = [o for o in text_objects(result.data) if "April" in o[0]]
    assert font.endswith("+TestSans") and font != "AAAAAA+TestSans"


def test_letters_no_font_has_refuse_the_edit_and_nothing_is_changed(
    contract: Path, no_fonts: InstalledFonts
) -> None:
    before = contract.read_bytes()

    with pytest.raises(EditFailed) as failed:
        apply_edits(contract, [Edit("12 March 2026", "5 April 2026")], fonts=no_fonts)

    assert "TestSans has no “A”, and" in str(failed.value)
    assert "isn't installed on this computer" in str(failed.value)
    assert str(failed.value).startswith("Nothing was changed.")
    assert contract.read_bytes() == before


def test_an_installed_font_whose_licence_forbids_embedding_isnt_used(
    tmp_path: Path, contract: Path
) -> None:
    folder = tmp_path / "restricted"
    folder.mkdir()
    build_font(folder / "TestSans.ttf", "TestSans", fs_type=0x0002)

    with pytest.raises(EditFailed, match="licence doesn't allow embedding"):
        apply_edits(contract, [Edit("12 March", "5 April")], fonts=InstalledFonts([folder]))


def test_two_edits_needing_the_installed_font_each_draw_their_own_letters(
    tmp_path: Path, contract: Path
) -> None:
    """PDFium reuses a font loaded under a name it has seen; each subset gets its own name,
    in the Windows name record too (Apple's fonts often lack it)."""
    folder = tmp_path / "mac-only-names"
    folder.mkdir()
    build_font(folder / "TestSans.ttf", "TestSans", windows_names=False)

    result = apply_edits(
        contract,
        [Edit("12 March", "5 April"), Edit("Maria", "Quinn"), Edit("two", "XYZ")],
        fonts=InstalledFonts([folder]),
    )

    text = page_text(result.data)
    assert "Made on 5 April 2026" in text
    assert "and Quinn Kovalenko" in text
    assert "payable in XYZ" in text
    added = {font for _, font, *_ in text_objects(result.data) if font != "AAAAAA+TestSans"}
    added.discard("BAAAAA+TestSans-Bold")
    assert len(added) == 3 and all(font.endswith("+TestSans") for font in added)


def test_a_wider_edit_moves_the_rest_of_the_line_and_keeps_its_spacing(
    contract: Path, with_fonts: InstalledFonts
) -> None:
    gap = ink_gap(contract.read_bytes(), "1,250.00", ", payable")

    result = apply_edits(contract, [Edit("1,250.00", "1,250,000.00")], fonts=with_fonts)

    assert ink_gap(result.data, "1,250,000.00", ", payable") == pytest.approx(gap, abs=0.1)
    assert "The total fee is EUR 1,250,000.00, payable in two" in page_text(result.data)


def test_text_after_a_wide_gap_is_a_column_and_stays_where_it_was(
    contract: Path, no_fonts: InstalledFonts
) -> None:
    price = boxes(contract.read_bytes(), "EUR 150.00")[0]

    result = apply_edits(contract, [Edit("Birth certificate", "Birth")], fonts=no_fonts)

    assert boxes(result.data, "EUR 150.00")[0] == pytest.approx(price, abs=0.01)
    assert result.changes[0].degraded == []


def test_running_into_the_next_column_is_reported(contract: Path, no_fonts: InstalledFonts) -> None:
    longer = "Birth certificate with an apostille attached"
    assert 72 + width_pt(longer) > 300  # reaches the price column

    result = apply_edits(contract, [Edit("Birth certificate", longer)], fonts=no_fonts)

    [degraded] = result.changes[0].degraded
    assert "into the text beside it" in degraded
    assert result.degraded == [f"p1 “{longer} EUR 150.00”: {degraded}"]


def test_running_past_the_right_margin_is_reported(
    contract: Path, with_fonts: InstalledFonts
) -> None:
    result = apply_edits(
        contract,
        [Edit("payable in two", "payable in two equal parts, the first on signing")],
        fonts=with_fonts,
    )

    [degraded] = result.changes[0].degraded
    assert "past the right margin" in degraded


def test_an_edit_across_two_fonts_is_drawn_in_the_first_and_says_so(
    contract: Path, with_fonts: InstalledFonts
) -> None:
    result = apply_edits(contract, [Edit("Trading (the", "Trading Ltd (the")], fonts=with_fonts)

    [degraded] = result.changes[0].degraded
    assert "drawn in 2 fonts" in degraded and "now in BAAAAA+TestSans-Bold" in degraded


def test_a_word_split_across_objects_is_one_word(
    contract: Path, with_fonts: InstalledFonts
) -> None:
    """Kerning splits "Trading" into "Acme T" + "rading": found and replaced as one."""
    result = apply_edits(contract, [Edit("Acme Trading", "Zed Co")], fonts=with_fonts)

    assert result.changes[0].before == "Made on 12 March 2026 by Acme Trading (the"
    assert "Made on 12 March 2026 by Zed Co (the" in page_text(result.data)
    assert ink_gap(result.data, "Zed Co", "(the") == pytest.approx(
        ink_gap(contract.read_bytes(), "Acme Trading", "(the"), abs=0.1
    )


def test_deleting_text_closes_the_gap(contract: Path, no_fonts: InstalledFonts) -> None:
    result = apply_edits(contract, [Edit("equal ", "")], fonts=no_fonts)

    assert "\ninstalments.\n" in page_text(result.data)


def test_cyrillic_text_is_changed_in_its_font(contract: Path, no_fonts: InstalledFonts) -> None:
    result = apply_edits(contract, [Edit("Київ", "Ківі")], fonts=no_fonts)

    assert "Договір укладено у місті Ківі." in page_text(result.data)


def test_rotated_text_is_refused(tmp_path: Path, font_files: Path) -> None:
    pdf = make_pdf(tmp_path / "r.pdf", font_files, [[[Run("Sideways", rotated=True)]]])

    with pytest.raises(EditFailed, match="rotated or slanted"):
        apply_edits(pdf, [Edit("Sideways", "Upright")], fonts=InstalledFonts([font_files]))


# --- What can't be opened or has no text -----------------------------------------------------


def test_a_page_without_text_is_said_to_be_a_scan(tmp_path: Path, font_files: Path) -> None:
    pdf = make_pdf(tmp_path / "scan.pdf", font_files, [[]])

    with pytest.raises(EditFailed, match="no text layer"):
        apply_edits(pdf, [Edit("anything", "else")], fonts=InstalledFonts([]))


@pytest.mark.parametrize(
    "data",
    [b"not a pdf at all", b"%PDF-1.7\n1 0 obj\n<<", b""],
    ids=["text", "cut off", "empty"],
)
def test_something_that_isnt_a_pdf_is_refused(tmp_path: Path, data: bytes) -> None:
    path = tmp_path / "broken.pdf"
    path.write_bytes(data)

    with pytest.raises(EditFailed, match="couldn't be opened as a PDF"):
        apply_edits(path, [Edit("a", "b")], fonts=InstalledFonts([]))


def test_a_password_protected_pdf_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "locked.pdf"
    path.write_bytes(_encrypted_pdf())

    with pytest.raises(EditFailed, match="password-protected"):
        apply_edits(path, [Edit("a", "b")], fonts=InstalledFonts([]))


def _encrypted_pdf() -> bytes:
    """A PDF with the standard security handler whose user password isn't empty: PDFium
    can't open it without one."""
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >>",
        b"<< /Filter /Standard /V 1 /R 2 /P -4 /O <"
        + b"11" * 32
        + b"> /U <"
        + b"22" * 32
        + b"> >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 5\n0000000000 65535 f \n" + b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += (
        b"trailer\n<< /Size 5 /Root 1 0 R /Encrypt 4 0 R /ID [<"
        + b"33" * 16
        + b"><"
        + b"33" * 16
        + b">] >>\nstartxref\n%d\n%%%%EOF\n" % xref
    )
    return bytes(out)


# --- Matching `find` -------------------------------------------------------------------------


def test_find_a_letter_off_is_answered_with_the_exact_text_and_not_changed(
    contract: Path, no_fonts: InstalledFonts
) -> None:
    """The 4B wrote "місто Київ" → "місто Львів" for "місті Київ": applied, its slip went
    into the document (agent-check, docs/acceptance/pdf-edit.md)."""
    with pytest.raises(EditFailed) as failed:
        apply_edits(contract, [Edit("місто Київ", "місто Ківі")], fonts=no_fonts)

    assert "It isn't in the PDF exactly; page 1 has “місті Київ”." in str(failed.value)
    assert "write `replace` to match it" in str(failed.value)


def test_find_on_the_wrong_page_is_taken_from_the_page_it_is_on(
    tmp_path: Path, font_files: Path
) -> None:
    pdf = make_pdf(
        tmp_path / "two.pdf", font_files, [[[Run("Page one text")]], [[Run("Total 250")]]]
    )

    result = apply_edits(
        pdf, [Edit("Total 250", "Total 520", page=1)], fonts=InstalledFonts([font_files])
    )

    assert result.changes[0].page == 2
    assert result.changes[0].notes == ["found on page 2, not page 1"]
    assert "Total 520" in page_text(result.data, 1)


def test_find_in_several_places_is_refused_with_the_places(
    contract: Path, no_fonts: InstalledFonts
) -> None:
    with pytest.raises(EditFailed) as failed:
        apply_edits(contract, [Edit("EUR", "USD")], fonts=no_fonts)

    message = str(failed.value)
    assert "It is in 2 places" in message
    assert "“The total fee is EUR 1,250.00, payable in two”" in message
    assert "“Birth certificate EUR 150.00”" in message
    assert "set `all`" in message


def test_all_changes_every_place(contract: Path, with_fonts: InstalledFonts) -> None:
    result = apply_edits(contract, [Edit("EUR", "UAH", all=True)], fonts=with_fonts)

    text = page_text(result.data)
    assert "EUR" not in text
    assert "fee is UAH 1,250.00" in text and "UAH 150.00" in text
    assert [c.after for c in result.changes] == [
        "The total fee is UAH 1,250.00, payable in two",
        "Birth certificate UAH 150.00",
    ]


def test_all_changes_every_place_on_one_line(tmp_path: Path, font_files: Path) -> None:
    pdf = make_pdf(tmp_path / "l.pdf", font_files, [[[Run("a cat, a cat and a cat")]]])

    result = apply_edits(pdf, [Edit("cat", "dog", all=True)], fonts=InstalledFonts([font_files]))

    assert "a dog, a dog and a dog" in page_text(result.data)


def test_find_that_isnt_there_names_the_nearest_lines(
    contract: Path, no_fonts: InstalledFonts
) -> None:
    with pytest.raises(EditFailed) as failed:
        apply_edits(contract, [Edit("total price is", "sum is")], fonts=no_fonts)

    message = str(failed.value)
    assert "It isn't in the PDF. The nearest lines:" in message
    assert "page 1: “The total fee is EUR 1,250.00, payable in two”" in message


def test_find_across_two_lines_asks_for_one_edit_per_line(
    contract: Path, no_fonts: InstalledFonts
) -> None:
    with pytest.raises(EditFailed, match="runs across two lines"):
        apply_edits(contract, [Edit("in two equal", "in three equal")], fonts=no_fonts)


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        (Edit("x", "y", page=9), "There is no page 9: the PDF has 1."),
        (Edit("", "y"), "`find` is empty."),
        (Edit("Maria", "Maria"), "`find` and `replace` are the same."),
        (Edit("Director", "Director\n\nConfidentiality: …"), "`replace` has a line break."),
        (Edit("two", "two\r\nthree"), "`replace` has a line break."),
    ],
    ids=["no such page", "empty find", "same", "a clause", "crlf"],
)
def test_impossible_edits_say_why(
    contract: Path, no_fonts: InstalledFonts, edit: Edit, message: str
) -> None:
    with pytest.raises(EditFailed, match=message.replace(".", r"\.")):
        apply_edits(contract, [edit], fonts=no_fonts)


def test_one_impossible_edit_stops_all_and_names_only_that_one(
    contract: Path, no_fonts: InstalledFonts
) -> None:
    with pytest.raises(EditFailed) as failed:
        apply_edits(
            contract,
            [Edit("Maria", "Mario"), Edit("nowhere to be found", "x")],
            fonts=no_fonts,
        )

    message = str(failed.value)
    assert message.startswith("Nothing was changed. “nowhere to be found” → “x”:")
    assert "Maria" not in message.split("nearest lines")[0]


def test_no_edits_is_an_error(contract: Path, no_fonts: InstalledFonts) -> None:
    with pytest.raises(EditFailed, match="No edits were given"):
        apply_edits(contract, [], fonts=no_fonts)
