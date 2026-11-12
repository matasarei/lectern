from __future__ import annotations

import struct
import zipfile
from pathlib import Path

import pytest

from lectern.projects import preview, text
from lectern.projects.preview import document_markdown, preview_kind
from lectern.projects.text import mime_for

from .conftest import write_docx, write_odt


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
    monkeypatch.setattr(text, "DOCUMENT_XML_LIMIT", 100)
    suffix = ".docx" if writer is write_docx else ".odt"

    with pytest.raises(text.DocumentTooLarge, match="has too much text to show here"):
        document_markdown(writer(tmp_path / f"lease{suffix}"))


def _damage_part(path: Path, part: str, *, cut: bool) -> Path:
    """The file with its compressed `part` damaged in place, or cut off part-way."""
    with zipfile.ZipFile(path) as archive:
        info = archive.getinfo(part)
    data = bytearray(path.read_bytes())
    name_len, extra_len = struct.unpack(
        "<HH", data[info.header_offset + 26 : info.header_offset + 30]
    )
    start = info.header_offset + 30 + name_len + extra_len
    if cut:
        data = data[: start + info.compress_size // 2]
    else:
        for i in range(start + 2, start + 40):
            data[i] ^= 0xFF
    path.write_bytes(bytes(data))
    return path


@pytest.mark.parametrize("cut", [False, True], ids=["damaged", "cut-off"])
@pytest.mark.parametrize(
    ("writer", "name", "part"),
    [(write_docx, "lease.docx", "word/document.xml"), (write_odt, "lease.odt", "content.xml")],
)
def test_a_document_with_a_damaged_compressed_part_says_it_couldnt_be_read(
    tmp_path, writer, name, part, cut
):
    path = _damage_part(writer(tmp_path / name), part, cut=cut)

    with pytest.raises(ValueError, match=f"{name} couldn't be read as a document"):
        document_markdown(path)


def test_a_docx_whose_main_part_has_another_name_is_still_read(tmp_path):
    # python-docx finds the main part through the package's relationships, not by its name.
    original = write_docx(tmp_path / "original.docx")
    renamed = tmp_path / "renamed.docx"
    moves = {
        "word/document.xml": "word/main.xml",
        "word/_rels/document.xml.rels": "word/_rels/main.xml.rels",
    }
    with (
        zipfile.ZipFile(original) as source,
        zipfile.ZipFile(renamed, "w", zipfile.ZIP_DEFLATED) as out,
    ):
        for item in source.infolist():
            data = source.read(item.filename)
            if item.filename in ("[Content_Types].xml", "_rels/.rels"):
                data = data.replace(b"word/document.xml", b"word/main.xml")
            out.writestr(moves.get(item.filename, item.filename), data)

    assert document_markdown(renamed).markdown.startswith("# Договір оренди")


def test_every_xml_part_counts_toward_the_size_limit(tmp_path, monkeypatch):
    path = write_odt(tmp_path / "lease.odt")
    with zipfile.ZipFile(path) as archive:
        sizes = {i.filename: i.file_size for i in archive.infolist()}
    # Room for content.xml alone, not for it and styles.xml together.
    monkeypatch.setattr(text, "DOCUMENT_XML_LIMIT", sizes["content.xml"] + 1)

    with pytest.raises(text.DocumentTooLarge):
        document_markdown(path)
