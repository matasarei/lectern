from __future__ import annotations

from lectern.modules.ocr.document import Block, Document, Doubt, PageInfo, Source, render_markdown
from lectern.modules.ocr.review import Decision, apply_decisions


def _doc(*doubts: tuple[str, str]) -> Document:
    """Page 3: one paragraph holding the doubts given as (id, raw)."""
    text = " ".join(f"{{{i}}}" for i, _ in doubts)
    block = Block(id="b3.1", type="paragraph", page=3, text=text,
                  doubts=[Doubt(id=i, raw=r, candidates=["10"]) for i, r in doubts])  # fmt: skip
    return Document(title="L", source=Source(artifact="l.pdf", pages=3),
                    pages=[PageInfo(n=3, image="p", size=(1, 1), blocks=["b3.1"])],
                    blocks=[block])  # fmt: skip


def _decision(doubt_id: str, raw: str, value: str, page: int = 3) -> Decision:
    return Decision(page=page, doubt_id=doubt_id, raw=raw, value=value, action="edit")


def test_a_decision_resolves_its_doubt_and_the_markdown_shows_the_value():
    doc, unplaced = apply_decisions(_doc(("d3.1", "1?")), [_decision("d3.1", "1?", "15")])

    (doubt,) = doc.blocks[0].doubts
    assert (doubt.status, doubt.value, doubt.resolved_by) == ("resolved", "15", "person")
    assert unplaced == []
    assert "{#b3.1 p=3} 15" in render_markdown(doc) and "⟦" not in render_markdown(doc)
    assert doc.open_doubts() == []


def test_a_decision_follows_its_text_when_cleaning_again_renumbered_the_doubts():
    # It was d3.1 when decided; the page was cleaned again and "1?" is now d3.2.
    doc, unplaced = apply_decisions(_doc(("d3.1", "Хр?щатик"), ("d3.2", "1?")),
                                    [_decision("d3.1", "1?", "15")])  # fmt: skip

    first, second = doc.blocks[0].doubts
    assert (first.status, second.status, second.value) == ("open", "resolved", "15")
    assert unplaced == []


def test_the_same_id_with_other_text_is_not_that_decision():
    doc, unplaced = apply_decisions(_doc(("d3.1", "2?")), [_decision("d3.1", "1?", "15")])
    assert doc.blocks[0].doubts[0].status == "open"
    assert [d.raw for d in unplaced] == ["1?"]  # its text is gone from the page


def test_a_decision_on_another_page_never_resolves_this_one():
    doc, unplaced = apply_decisions(_doc(("d3.1", "1?")), [_decision("d4.1", "1?", "15", 4)])
    assert doc.blocks[0].doubts[0].status == "open" and len(unplaced) == 1


def test_two_doubts_on_the_same_text_each_take_their_own_decision():
    doc, _ = apply_decisions(
        _doc(("d3.1", "1?"), ("d3.2", "1?")),
        [_decision("d3.2", "1?", "18"), _decision("d3.1", "1?", "15")],
    )
    assert [d.value for d in doc.blocks[0].doubts] == ["15", "18"]


def test_the_document_given_is_left_as_it_was():
    doc = _doc(("d3.1", "1?"))
    apply_decisions(doc, [_decision("d3.1", "1?", "15")])
    assert doc.blocks[0].doubts[0].status == "open"
