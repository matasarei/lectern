from __future__ import annotations

from lectern.documents.document import render_markdown
from lectern.documents.from_text import parse_markdown, parse_text

LETTER = """# Відповідь на лист

Шановний пане Мельник,
дякуємо за лист.

## Платежі

| Місяць | Сума |
|---|---:|
| березень | 250,00 |
| квітень | 250,00 |

- перше
* друге
1. третє

Text with {d1} and ⟦1? | open⟧ stays text.
"""


def test_markdown_becomes_blocks_on_one_page():
    doc = parse_markdown("reply-letter", LETTER, source="reply-letter.md")

    assert [(b.id, b.type) for b in doc.blocks] == [
        ("b1.1", "heading"), ("b1.2", "paragraph"), ("b1.3", "heading"),
        ("b1.4", "table"), ("b1.5", "list"), ("b1.6", "paragraph"),
    ]  # fmt: skip
    heading, para, sub, table, items, last = doc.blocks
    assert (heading.level, heading.text, sub.level) == (1, "Відповідь на лист", 2)
    assert para.text == "Шановний пане Мельник,\nдякуємо за лист."
    assert table.rows == [["Місяць", "Сума"], ["березень", "250,00"], ["квітень", "250,00"]]
    assert items.text == "перше\nдруге\nтретє"
    assert last.text == "Text with {d1} and ⟦1? | open⟧ stays text." and last.doubts == []
    assert (doc.title, doc.source.artifact, doc.source.pages) == (
        "reply-letter",
        "reply-letter.md",
        1,
    )
    assert doc.pages[0].blocks == [b.id for b in doc.blocks]
    assert doc.open_doubts() == []


def test_a_table_cell_with_an_escaped_bar_keeps_it():
    doc = parse_markdown("t", "| a \\| b | c |\n|---|---|\n| 1 | 2 |\n")
    assert doc.blocks[0].rows == [["a | b", "c"], ["1", "2"]]


def test_plain_text_is_paragraphs_split_on_blank_lines():
    doc = parse_text("notes", "Перший абзац\nпродовження.\n\n\nДругий.\n", source="notes.txt")
    assert [b.text for b in doc.blocks] == ["Перший абзац\nпродовження.", "Другий."]


def test_empty_text_is_an_empty_document():
    assert parse_markdown("e", "").blocks == [] and parse_text("e", "  \n").blocks == []
    assert "document: e" in render_markdown(parse_text("e", ""))


def test_pages_become_a_document_with_the_pdfs_page_numbers():
    from lectern.documents.from_text import parse_pages

    statement = "Date Amount\n06.10 -12,50\n07.10 +100,00"
    doc = parse_pages(
        "statement", ["Account 123\n\nOpening balance", "", statement], source="statement.pdf"
    )

    assert doc.source.artifact == "statement.pdf" and doc.source.pages == 3
    assert [(p.n, p.blocks) for p in doc.pages] == [(1, ["b1.1", "b1.2"]), (2, []), (3, ["b3.1"])]
    assert [(b.id, b.page, b.type) for b in doc.blocks] == [
        ("b1.1", 1, "paragraph"), ("b1.2", 1, "paragraph"), ("b3.1", 3, "paragraph"),
    ]  # fmt: skip
    assert doc.blocks[2].text == statement  # a statement's rows stay one per line
