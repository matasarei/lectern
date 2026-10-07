"""The shared exporter tests (blueprint: every exporter passes one set of test documents).

Each installed exporter writes the shared lease; its output is read back with a reader for
its format, and the same things must hold: the table that ran across pages is whole, an
answered question is its value, an open question is a comment with its candidates (or an
inline mark where the format has no comments), the figure is a visible placeholder, and
page notes appear only when asked for."""

from __future__ import annotations

import io
from dataclasses import dataclass, field

import pytest

from lectern.documents.export import Exporter, ExportOptions, resolved_options
from lectern.documents.from_text import parse_markdown
from lectern.modules import load_modules, module_exporters

from .fixtures import lease


@dataclass
class Read:
    body: str  # all the text, paragraphs and cells, in order
    tables: list[list[list[str]]] = field(default_factory=list)
    comments: list[str] = field(default_factory=list)


def _read_md(data: bytes) -> Read:
    text = data.decode("utf-8")
    tables = [b.rows for b in parse_markdown("x", text).blocks if b.type == "table"]
    return Read(text, tables)


def _read_docx(data: bytes) -> Read:
    import docx
    from docx.table import Table

    document = docx.Document(io.BytesIO(data))
    body, tables = [], []
    for item in document.iter_inner_content():
        if isinstance(item, Table):
            rows = [[cell.text for cell in row.cells] for row in item.rows]
            tables.append(rows)
            body += [cell for row in rows for cell in row]
        else:
            body.append(item.text)
    return Read("\n".join(body), tables, [c.text for c in document.comments])


READERS = {"md": _read_md, "docx": _read_docx}


def _exporters() -> list[Exporter]:
    return module_exporters(load_modules())


def _formats() -> list[str]:
    return [e.format for e in _exporters()]


def _export(fmt: str, **opts: str) -> tuple[Exporter, Read]:
    (exporter,) = [e for e in _exporters() if e.format == fmt]
    options = resolved_options(ExportOptions(**opts), exporter)
    data = exporter.export(lease(), options)
    assert isinstance(data, bytes) and data
    return exporter, READERS[fmt](data)


def test_every_exporter_has_a_reader_here_and_the_formats_are_installed():
    assert {"md", "docx"} <= set(_formats()) and set(_formats()) <= set(READERS)


@pytest.mark.parametrize("fmt", _formats())
def test_the_table_that_ran_across_pages_is_whole(fmt):
    _, read = _export(fmt)
    (table,) = read.tables
    assert table == [["Період", "Сума, грн"], ["березень 1998", "250,00"],
                     ["квітень 1998", "250,00"], ["Разом", "750,00"]]  # fmt: skip


@pytest.mark.parametrize("fmt", _formats())
def test_an_answered_question_is_its_value_and_its_raw_text_is_gone(fmt):
    _, read = _export(fmt)
    assert "250,00" in read.body and "2?0,00" not in read.body + "".join(read.comments)


@pytest.mark.parametrize("fmt", _formats())
def test_an_open_question_is_a_comment_where_the_format_has_them_else_an_inline_mark(fmt):
    exporter, read = _export(fmt)
    if "comments" in exporter.capabilities:
        assert "⟦" not in read.body and "Оплата не пізніше 1? числа" in read.body
        assert any("10 · 15 · 18" in c and "smudged digit" in c for c in read.comments)
    else:
        assert "Оплата не пізніше ⟦1? → 10 · 15 · 18⟧ числа" in read.body
    _, inline = _export(fmt, open_doubts="inline")
    assert "⟦1? → 10 · 15 · 18⟧" in inline.body


@pytest.mark.parametrize("fmt", _formats())
def test_the_figure_is_a_visible_placeholder_and_the_heading_and_text_are_there(fmt):
    _, read = _export(fmt)
    for text in (
        "[Image 1: stamp, page 3]",
        "3. Орендна плата",
        "Договір діє до 31 грудня 1999 року.",
    ):
        assert text in read.body


@pytest.mark.parametrize("fmt", _formats())
def test_page_notes_appear_only_when_asked_for(fmt):
    _, plain = _export(fmt)
    assert "landlord" not in plain.body + "".join(plain.comments)
    exporter, asked = _export(fmt, notes="comment")
    where = asked.comments if "comments" in exporter.capabilities else [asked.body]
    assert any("Note for page 3: The stamp is the landlord's." in w for w in where)
