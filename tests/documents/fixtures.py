"""The shared test document every exporter must pass (docs/ocr.md, "Exports"): a table
that spans pages, open and resolved questions, notes, and a figure.
A plain module: importing a test file would run its tests again in the importer."""

from __future__ import annotations

from lectern.documents.document import Block, Document, Doubt, PageInfo, Source


def lease() -> Document:
    """Pages 3-4 of a lease: a heading; a paragraph with an open question; a table that runs
    from page 3 onto page 4 with an answered question in a cell; a stamp (a figure, no
    cut-out); a page note; and a closing paragraph on page 4."""
    return Document(
        title="Lease 1998",
        version=7,
        source=Source(artifact="lease-1998-scan.pdf", pages=2),
        pages=[
            PageInfo(
                n=3,
                image="pages/p003.png",
                size=(10, 10),
                note="The stamp is the landlord's.",
                blocks=["b3.1", "b3.2", "b3.3", "b3.4"],
            ),
            PageInfo(n=4, image="pages/p004.png", size=(10, 10), blocks=["b3.3", "b4.1"]),
        ],  # fmt: skip
        blocks=[
            Block(id="b3.1", type="heading", page=3, level=2, text="3. Орендна плата"),
            Block(
                id="b3.2",
                type="paragraph",
                page=3,
                text="Оплата не пізніше {d3.1} числа поточного місяця.",
                doubts=[
                    Doubt(
                        id="d3.1", raw="1?", candidates=["10", "15", "18"], reason="smudged digit"
                    )
                ],
            ),
            Block(
                id="b3.3",
                type="table",
                page=3,
                pages=[3, 4],
                rows=[
                    ["Період", "Сума, грн"],
                    ["березень 1998", "250,00"],
                    ["квітень 1998", "{d4.1}"],
                    ["Разом", "750,00"],
                ],
                doubts=[
                    Doubt(
                        id="d4.1",
                        raw="2?0,00",
                        status="resolved",
                        value="250,00",
                        candidates=["250,00"],
                        resolved_by="person",
                    )
                ],
            ),
            Block(id="b3.4", type="figure", page=3, label="stamp"),
            Block(id="b4.1", type="paragraph", page=4, text="Договір діє до 31 грудня 1999 року."),
        ],  # fmt: skip
    )
