from __future__ import annotations

import json

from lectern.documents.document import Block, Document, Doubt, PageInfo, Source, render_markdown


def _lease() -> Document:
    return Document(
        title="Lease 1998",
        version=7,
        source=Source(artifact="lease-1998-scan.pdf", pages=12),
        pages=[
            PageInfo(
                n=3,
                image="pages/p003.png",
                size=(2480, 3508),
                note="The stamp is the landlord's.",
                blocks=["b3.1", "b3.2", "b3.3", "b3.4", "b3.5"],
            ),
            PageInfo(n=4, image="pages/p004.png", size=(2480, 3508), blocks=["b3.4"]),
        ],
        blocks=[
            Block(
                id="b3.1",
                type="heading",
                page=3,
                level=2,
                text="3. Орендна плата та порядок розрахунків",
            ),
            Block(
                id="b3.2",
                type="table",
                page=3,
                rows=[
                    ["Період", "Сума, грн"],
                    ["березень 1998", "250,00"],
                    ["квітень 1998", "2{d2}0,00"],
                ],
                doubts=[Doubt(id="d2", raw="?", status="resolved", value="5", resolved_by="you")],
            ),
            Block(
                id="b3.3",
                type="paragraph",
                page=3,
                text="Оплата здійснюється не пізніше {d1} числа поточного місяця.",
                doubts=[
                    Doubt(
                        id="d1",
                        raw="1?",
                        candidates=["10", "15", "18"],
                        reason="the second digit is smudged",
                    )
                ],
            ),
            Block(
                id="b3.4",
                type="paragraph",
                page=3,
                pages=[3, 4],
                text="У разі прострочення платежу нараховується пеня.",
            ),
            Block(id="b3.5", type="figure", page=3, label="stamp"),
        ],
    )


def test_the_document_round_trips_through_json_with_its_schema_key():
    doc = _lease()
    raw = json.loads(doc.to_json())

    assert raw["schema"] == "lectern.document/1" and raw["version"] == 7
    assert raw["blocks"][2]["bbox"] is None  # no layout model yet
    assert Document.from_json(doc.to_json()) == doc
    assert [d.id for d in doc.open_doubts()] == ["d1"]


def test_document_md_has_anchors_tables_inline_doubts_notes_and_placeholders():
    md = render_markdown(_lease())

    assert md.startswith(
        "---\ndocument: Lease 1998 · version 7\nsource: lease-1998-scan.pdf · 12 pages\n"
        "open_questions: 1\n---\n"
    )
    assert "> Note for page 3 (from you): The stamp is the landlord's." in md
    assert "## 3. Орендна плата та порядок розрахунків {#b3.1 p=3}" in md
    assert "{#b3.2 p=3 table}\n| Період | Сума, грн |\n|---|---|\n| березень 1998 | 250,00 |" in md
    assert "| квітень 1998 | 250,00 |" in md  # a resolved doubt shows its reading
    assert "{#b3.3 p=3} Оплата здійснюється не пізніше ⟦1? | open | 10 · 15 · 18⟧ числа" in md
    assert "{#b3.4 p=3→4} У разі прострочення" in md
    assert md.count("{#b3.4") == 1  # written once, on its first page
    assert "{#b3.5 p=3 figure} [Image 1: stamp · no cut-out yet]" in md


def test_a_page_that_couldnt_be_cleaned_shows_a_placeholder():
    doc = Document(
        title="Scan",
        source=Source(artifact="scan.pdf", pages=1),
        pages=[
            PageInfo(n=1, image="pages/p001.png", size=(10, 10), failed="the reply wasn't valid")
        ],
    )
    assert "{#p1 p=1} [Page 1 wasn't cleaned: the reply wasn't valid]" in render_markdown(doc)


def test_the_open_question_count_follows_a_new_document_json(tmp_path):
    import os

    from lectern.modules.ocr.store import DOCUMENT_JSON, open_doubt_count
    from lectern.projects.tables import OcrSessionRow

    ocr = OcrSessionRow(folder="lease")
    path = tmp_path / "lease" / DOCUMENT_JSON
    assert open_doubt_count(tmp_path, ocr) == 0  # no Document yet

    path.parent.mkdir()
    doc = _lease()
    path.write_text(doc.to_json(), encoding="utf-8")
    assert open_doubt_count(tmp_path, ocr) == 1

    doc.blocks[2].doubts[0].status = "resolved"  # d1, as M3b's review does
    path.write_text(doc.to_json(), encoding="utf-8")
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    assert open_doubt_count(tmp_path, ocr) == 0
