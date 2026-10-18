from __future__ import annotations

from lectern.projects import text
from lectern.projects.text import extract, mime_for

from .conftest import add_pages, write_docx, write_pdf, write_scan_pdf


def test_markdown_is_chunked_by_heading(tmp_path):
    path = tmp_path / "notes.md"
    path.write_text("Intro line.\n\n# Terms\nPay by the 10th.\n\n## Penalty\n0,5% a day.\n")

    got = extract(path)

    assert [(c.ref, c.heading, c.body) for c in got.chunks] == [
        ("#c1", "", "Intro line."),
        ("#c2", "Terms", "Pay by the 10th."),
        ("#c3", "Penalty", "0,5% a day."),
    ]
    assert got.note is None


def test_long_text_is_cut_on_paragraphs(tmp_path):
    paragraph = "word " * 100  # 500 characters
    path = tmp_path / "long.txt"
    path.write_text("\n\n".join([paragraph] * 7))

    chunks = extract(path).chunks

    assert all(len(c.body) <= text.CHUNK_CHARS for c in chunks)
    assert len(chunks) == 4  # two 500-character paragraphs per chunk: a third would pass 1,500
    assert "".join(c.body.replace("\n\n", "") for c in chunks).count("word") == 700


def test_a_paragraph_longer_than_a_chunk_is_split_hard(tmp_path):
    path = tmp_path / "one.txt"
    path.write_text("x" * (text.CHUNK_CHARS * 2 + 10))
    assert [len(c.body) for c in extract(path).chunks] == [
        text.CHUNK_CHARS, text.CHUNK_CHARS, 10
    ]  # fmt: skip


def test_windows_1251_text_is_read(tmp_path):
    path = tmp_path / "old.txt"
    path.write_bytes("Договір оренди".encode("cp1251"))
    assert extract(path).chunks[0].body == "Договір оренди"


def test_binary_and_unknown_files_have_no_text(tmp_path):
    binary = tmp_path / "data.txt"
    binary.write_bytes(b"\x00\x01\x02" * 100)
    image = tmp_path / "stamp.png"
    image.write_bytes(b"\x89PNG\r\n")
    assert extract(binary).chunks == [] and extract(binary).note == "not a text file"
    assert extract(image).chunks == [] and extract(image).note


def test_text_over_the_limit_is_cut_with_a_note(tmp_path, monkeypatch):
    monkeypatch.setattr(text, "TEXT_LIMIT", 100)
    path = tmp_path / "big.txt"
    path.write_text("a" * 250)
    got = extract(path)
    assert sum(len(c.body) for c in got.chunks) == 100
    assert got.note and "first" in got.note


def test_docx_keeps_headings_and_tables(tmp_path):
    got = extract(write_docx(tmp_path / "lease.docx"))

    assert [c.heading for c in got.chunks] == ["Договір оренди", "Підписи"]
    assert "Пеня | 0,5% на день" in got.chunks[0].body
    assert "прострочення" in got.chunks[0].body


def test_pdf_text_layer_is_read_per_page(tmp_path):
    got = extract(
        write_pdf(tmp_path / "terms.pdf", ["Payment is due by the tenth", "Penalty clause"])
    )
    assert [(c.heading, c.body) for c in got.chunks] == [
        ("Page 1", "Payment is due by the tenth"),
        ("Page 2", "Penalty clause"),
    ]


def test_a_scan_has_no_text_layer(tmp_path):
    got = extract(write_scan_pdf(tmp_path / "scan.pdf"))
    assert got.chunks == []
    assert "no text layer" in (got.note or "")


def test_a_scanned_page_has_an_image_and_no_text_and_a_blank_page_isnt_one(tmp_path):
    from lectern.projects.text import scanned_pages

    typed = write_pdf(tmp_path / "letter.pdf", ["Dear tenant, the rent is due on the tenth."])
    assert scanned_pages(add_pages(typed, blank=2)) == 0  # a typed letter with blank pages
    assert scanned_pages(add_pages(typed, scanned=1)) == 1
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"%PDF-1.4 not really")
    assert scanned_pages(broken) == 0


def test_a_broken_pdf_is_reported(tmp_path):
    path = tmp_path / "broken.pdf"
    path.write_bytes(b"%PDF-1.4 not really")
    assert extract(path).note == "the PDF couldn't be opened"


def test_mime_types():
    assert mime_for("a.md") == "text/markdown"
    assert mime_for("a.docx").endswith("wordprocessingml.document")
    assert mime_for("a.pdf") == "application/pdf"
    assert mime_for("a.unknownext") == "application/octet-stream"


def test_pdfs_are_read_one_at_a_time(tmp_path, monkeypatch):
    import threading
    import time

    pdfs = [
        write_pdf(tmp_path / f"{i}.pdf", [f"Page text number {i} for the check"]) for i in range(6)
    ]
    inside, most = 0, 0
    guard = threading.Lock()
    real = text._pdf_text

    def counting(path):
        nonlocal inside, most
        with guard:
            inside += 1
            most = max(most, inside)
        try:
            time.sleep(0.05)  # hold the section open, so threads without the lock would overlap
            return real(path)
        finally:
            with guard:
                inside -= 1

    monkeypatch.setattr(text, "_pdf_text", counting)
    threads = [threading.Thread(target=extract, args=(p,)) for p in pdfs]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert most == 1


def test_a_document_md_is_one_chunk_per_block_labelled_with_its_id(tmp_path):
    from lectern.projects.text import extract

    path = tmp_path / "document.md"
    path.write_text(
        "---\ndocument: Lease · version 1\n---\n\n> Note for page 3 (from you): keep the table.\n\n"
        "## 3. Плата {#b3.1 p=3}\n\n{#b3.2 p=3 table}\n| А | Б |\n|---|---|\n| 1 | 2 |\n\n"
        "{#b3.3 p=3→4} Текст через дві сторінки.\n\n{#p4 p=4} [Page 4 wasn't cleaned: x]\n",
        encoding="utf-8",
    )
    chunks = extract(path).chunks

    assert [c.ref for c in chunks] == ["#c1", "#note3", "#b3.1", "#b3.2", "#b3.3", "#p4"]
    assert chunks[3].heading == "3. Плата" and chunks[3].body.startswith("| А | Б |")
    assert chunks[4].body == "Текст через дві сторінки."
    assert [c.heading for c in chunks[:2]] == ["About this document", "Page 3"]


def test_a_document_block_with_no_heading_before_it_is_in_its_pages_section(tmp_path):
    from lectern.projects.text import extract

    path = tmp_path / "document.md"
    path.write_text(
        "---\ndocument: Lease · version 1\n---\n\n{#b1.1 p=1} 1. Сторони\n\n"
        "{#b2.1 p=2→3} Текст.\n\n## 4. Права {#b3.1 p=3}\n\n{#b4.1 p=4} Ще.\n",
        encoding="utf-8",
    )
    headings = [c.heading for c in extract(path).chunks]
    assert headings == ["About this document", "Page 1", "Page 2", "4. Права", "4. Права"]


def test_pdf_pages_gives_every_pages_text_empty_ones_included(tmp_path):
    from lectern.projects.text import pdf_pages

    pdf = write_pdf(tmp_path / "terms.pdf", ["Payment is due by the tenth", "Penalty clause"])
    assert pdf_pages(pdf) == ["Payment is due by the tenth", "Penalty clause"]
    assert pdf_pages(write_scan_pdf(tmp_path / "scan.pdf", pages=3)) == ["", "", ""]


def test_pdf_pages_says_when_the_pdf_cant_be_opened(tmp_path):
    import pytest

    from lectern.projects.text import pdf_pages

    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"%PDF-1.4 not really")
    with pytest.raises(ValueError, match="couldn't be opened"):
        pdf_pages(broken)


def test_docx_markdown_writes_real_markdown_tables(tmp_path):
    from lectern.projects.text import docx_markdown

    got = docx_markdown(write_docx(tmp_path / "lease.docx"))
    assert "# Договір оренди" in got and "## Підписи" in got
    assert "| Сума | 250,00 грн |\n| --- | --- |\n| Пеня | 0,5% на день |" in got


def test_export_gets_every_page_even_past_the_index_limit(tmp_path, monkeypatch):
    from lectern.projects.text import pdf_pages

    monkeypatch.setattr(text, "TEXT_LIMIT", 30)
    pages = ["Payment is due by the tenth", "Penalty clause applies", "Signatures follow"]
    pdf = write_pdf(tmp_path / "long.pdf", pages)
    assert "only the first" in (extract(pdf).note or "")  # the index stops early, and says so
    assert pdf_pages(pdf) == pages  # an export doesn't: it would lose pages silently
