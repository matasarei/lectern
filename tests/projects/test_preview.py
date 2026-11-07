from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from lectern.projects import preview, text
from lectern.projects.preview import document_markdown, preview_kind
from lectern.projects.text import mime_for

from .conftest import write_docx


@pytest.mark.parametrize(
    ("name", "kind"),
    [
        ("notes.md", "markdown"),
        ("README.MARKDOWN", "markdown"),
        ("notes.txt", "text"),
        ("data.json", "text"),
        ("feed.xml", "text"),
        ("config.yaml", "text"),
        ("script.py", "text"),
        ("server.log", "text"),
        ("table.csv", "text"),
        ("page.html", "html"),
        ("PAGE.HTM", "html"),
        ("scan.pdf", "pdf"),
        ("contract.docx", "document"),
        ("contract.odt", "document"),
        ("old.doc", "legacy"),
        ("letter.rtf", "legacy"),
        ("photo.jpg", "image"),
        ("diagram.png", "image"),
        ("logo.svg", None),
        ("archive.zip", None),
        ("no-extension", None),
    ],
)
def test_each_file_has_the_preview_its_type_gets(name, kind):
    assert preview_kind(name, mime_for(name)) == kind


def test_html_is_never_a_plain_text_preview():
    # .html is in TEXT_EXTENSIONS and its type is text/html: still its own kind, so the page
    # offers the sandboxed tab and never treats it as ordinary text.
    assert preview_kind("page.html", "text/html") == "html"


def write_odt(path: Path) -> Path:
    from odfdo import Document, Header, List, ListItem, Paragraph, Table

    document = Document("text")
    document.body.append(Header(1, "Договір оренди"))
    document.body.append(Paragraph("Орендар сплачує *пеню* за кожен день."))
    table = Table("Суми", width=2, height=2)
    table.set_value((0, 0), "Сума")
    table.set_value((1, 0), "250,00 грн")
    table.set_value((0, 1), "Пеня")
    table.set_value((1, 1), "0,5% на день")
    document.body.append(table)
    items = List()
    items.append(ListItem("Перший пункт"))
    document.body.append(items)
    document.save(path)
    return path


def test_a_docx_is_markdown_with_its_headings_and_tables(tmp_path):
    markdown = document_markdown(write_docx(tmp_path / "lease.docx")).markdown

    assert "# Договір оренди" in markdown
    assert "## Підписи" in markdown
    assert "| Сума | 250,00 грн |" in markdown
    assert "| --- | --- |" in markdown


def test_an_odt_is_markdown_with_its_headings_tables_and_lists(tmp_path):
    markdown = document_markdown(write_odt(tmp_path / "lease.odt")).markdown

    assert "# Договір оренди" in markdown
    assert "\\*пеню\\*" in markdown  # its own asterisks stay text
    assert "| Сума | 250,00 грн" in markdown
    assert "Перший пункт" in markdown


@pytest.mark.parametrize(
    "name", ["junk.docx", "junk.odt", "empty.docx", "empty.odt", "bad-xml.odt"]
)
def test_a_broken_document_says_it_couldnt_be_read(tmp_path, name):
    path = tmp_path / name
    if name.startswith("junk"):
        path.write_bytes(b"not a zip at all")
    else:
        with zipfile.ZipFile(path, "w") as archive:
            if name.startswith("bad-xml"):
                archive.writestr("mimetype", "application/vnd.oasis.opendocument.text")
                archive.writestr("content.xml", "<oops")
                archive.writestr("META-INF/manifest.xml", "<oops")
            else:
                archive.writestr("notes.txt", "no document part in here")

    with pytest.raises(ValueError, match=f"{name} couldn't be read as a document"):
        document_markdown(path)


def test_only_docx_and_odt_are_converted(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text("hello")
    with pytest.raises(ValueError, match="isn't a DOCX or ODT file"):
        document_markdown(path)


def test_a_short_document_is_shown_whole(tmp_path):
    assert document_markdown(write_docx(tmp_path / "lease.docx")).cut is False


@pytest.mark.parametrize("writer", [write_docx, write_odt])
def test_only_the_beginning_of_a_long_document_is_shown(tmp_path, monkeypatch, writer):
    monkeypatch.setattr(preview, "DOCUMENT_PREVIEW_CHARS", 30)
    suffix = ".docx" if writer is write_docx else ".odt"

    got = document_markdown(writer(tmp_path / f"lease{suffix}"))

    assert got.cut is True
    assert got.markdown.startswith("# Договір оренди")
    assert len(got.markdown) <= 30
    assert "Орендар" not in got.markdown  # cut at a line's end, not in the middle of one


def test_a_docx_walk_stops_once_past_its_limit(tmp_path):
    import docx

    document = docx.Document()
    for n in range(1000):
        document.add_paragraph(f"Paragraph {n} " + "x" * 90)
    document.save(str(tmp_path / "long.docx"))

    markdown = text.docx_markdown(tmp_path / "long.docx", limit=1000)

    assert "Paragraph 9 " in markdown
    assert "Paragraph 20 " not in markdown
    assert "Paragraph 999 " in text.docx_markdown(tmp_path / "long.docx")  # no limit: all of it


@pytest.mark.parametrize("writer", [write_docx, write_odt])
def test_a_document_whose_text_unzips_too_big_isnt_opened(tmp_path, monkeypatch, writer):
    monkeypatch.setattr(preview, "DOCUMENT_XML_LIMIT", 100)
    suffix = ".docx" if writer is write_docx else ".odt"

    with pytest.raises(preview.DocumentTooLarge, match="has too much text to show here"):
        document_markdown(writer(tmp_path / f"lease{suffix}"))
