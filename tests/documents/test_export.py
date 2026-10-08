from __future__ import annotations

import pytest

from lectern.documents.export import (
    ExportOptions,
    figure_placeholder,
    inline_doubt,
    load_for_export,
    lost,
    resolved_options,
    segments,
)

from .fixtures import lease


class _Fake:
    format, title, extension, mime = "fake", "Export · Fake", ".fake", "text/plain"

    def __init__(self, *capabilities: str) -> None:
        self.capabilities = frozenset(capabilities)

    def export(self, doc, opts):  # noqa: ANN001, ANN201
        return b""


def test_open_questions_default_to_comments_where_the_format_has_them():
    assert resolved_options(ExportOptions(), _Fake("comments")).open_doubts == "comment"
    assert resolved_options(ExportOptions(), _Fake("tables")).open_doubts == "inline"
    assert (
        resolved_options(ExportOptions(open_doubts="inline"), _Fake("comments")).open_doubts
        == "inline"
    )
    assert resolved_options(ExportOptions(), _Fake()).notes == "omit"


def test_a_text_splits_into_plain_runs_and_its_questions_answered_ones_read_as_their_value():
    doc = lease()
    para, table = doc.blocks[1], doc.blocks[2]
    pieces = segments(para.text, para.doubts)
    assert [(text, d.id if d else None) for text, d in pieces] == [
        ("Оплата не пізніше ", None), ("1?", "d3.1"), (" числа поточного місяця.", None),
    ]  # fmt: skip
    assert segments(table.rows[2][1], table.doubts) == [("250,00", None)]


def test_inline_marks_and_figure_placeholders_are_visible():
    doc = lease()
    assert inline_doubt(doc.blocks[1].doubts[0]) == "⟦1? → 10 · 15 · 18⟧"
    assert inline_doubt(doc.blocks[1].doubts[0].model_copy(update={"candidates": []})) == "⟦1?⟧"
    assert figure_placeholder(doc.blocks[3], 1) == "[Image 1: stamp, page 3]"


def test_only_what_a_format_cant_keep_is_named_as_lost():
    doc = lease()
    asked = ExportOptions(open_doubts="comment", notes="comment")
    assert lost(doc, asked, _Fake("comments", "tables")) == []
    assert lost(doc, asked, _Fake("tables")) == [
        "comments: the open questions become inline marks",
        "comments: the page notes become paragraphs",
    ]
    assert lost(doc, ExportOptions(), _Fake("tables")) == []  # nothing asked of it
    answered = doc.model_copy(deep=True)
    answered.blocks[1].doubts[0].status = "resolved"
    assert lost(answered, ExportOptions(open_doubts="comment"), _Fake()) == []


def test_a_documents_folder_or_a_text_file_loads_and_anything_else_says_why(tmp_path):
    folder = tmp_path / "Lease 1998"
    folder.mkdir()
    (folder / "document.json").write_text(lease().to_json(), encoding="utf-8")
    (folder / "document.md").write_text("…", encoding="utf-8")
    (tmp_path / "reply-letter.md").write_text("# Відповідь\n\nТекст.\n", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("Один.\n\nДва.\n", encoding="utf-8")
    (tmp_path / "photo.png").write_bytes(b"\x89PNG")

    assert load_for_export(tmp_path, "Lease 1998/document.md").title == "Lease 1998"
    letter = load_for_export(tmp_path, "reply-letter.md")
    assert (letter.title, letter.blocks[0].type) == ("reply-letter", "heading")
    assert [b.text for b in load_for_export(tmp_path, "notes.txt").blocks] == ["Один.", "Два."]
    # The message used to be asked of a PDF; a PDF with text exports now, so an image asks it.
    with pytest.raises(ValueError, match="isn't a Document, a PDF with text, a DOCX or a text"):
        load_for_export(tmp_path, "photo.png")


def test_a_pdf_with_text_loads_page_by_page_and_a_scan_or_a_broken_one_says_why(tmp_path):
    from tests.projects.conftest import write_pdf, write_scan_pdf

    write_pdf(tmp_path / "statement.pdf", ["Opening balance 100,00", "Closing balance 87,50"])
    doc = load_for_export(tmp_path, "statement.pdf")
    assert (doc.title, doc.source.artifact, doc.source.pages) == ("statement", "statement.pdf", 2)
    assert [(b.id, b.text) for b in doc.blocks] == [
        ("b1.1", "Opening balance 100,00"), ("b2.1", "Closing balance 87,50"),
    ]  # fmt: skip

    write_scan_pdf(tmp_path / "scan.pdf")
    with pytest.raises(ValueError, match="no text layer.*ocr.start"):
        load_for_export(tmp_path, "scan.pdf")
    (tmp_path / "broken.pdf").write_bytes(b"%PDF")
    with pytest.raises(ValueError, match="couldn't be opened"):
        load_for_export(tmp_path, "broken.pdf")


def test_a_docx_loads_with_its_headings_and_tables(tmp_path):
    from tests.projects.conftest import write_docx

    write_docx(tmp_path / "lease.docx")
    doc = load_for_export(tmp_path, "lease.docx")
    assert doc.title == "lease" and doc.source.artifact == "lease.docx"
    assert [b.type for b in doc.blocks] == ["heading", "paragraph", "table", "heading", "paragraph"]
    assert doc.blocks[2].rows == [["Сума", "250,00 грн"], ["Пеня", "0,5% на день"]]
