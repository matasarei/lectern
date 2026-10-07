from __future__ import annotations

from lectern.documents.document import render_markdown
from lectern.modules.ocr.assemble import PageInput, assemble
from lectern.modules.ocr.clean import CleanBlock, CleanDoubt, CleanedPage


def _page(n: int, *blocks: CleanBlock, doubts=(), note: str = "", unsure=()) -> PageInput:  # noqa: ANN001
    return PageInput(
        n=n,
        image=f"pages/p{n:03d}.png",
        size=(2480, 3508),
        note=note,
        unsure=list(unsure),
        cleaned=CleanedPage(blocks=list(blocks), doubts=list(doubts)),
    )


def para(text: str) -> CleanBlock:
    return CleanBlock(type="paragraph", text=text)


def table(*rows: list[str]) -> CleanBlock:
    return CleanBlock(type="table", rows=[list(r) for r in rows])


HEADER = para("Договір оренди № 7 від 1998 р.")


def test_blocks_get_page_ids_and_cleanups_doubts_take_their_place():
    doc = assemble(
        "Lease",
        "lease.pdf",
        [
            _page(
                3,
                CleanBlock(type="heading", level=2, text="3. Орендна плата"),
                para("Оплата не пізніше 1? числа."),
                doubts=[CleanDoubt(block=1, raw="1?", candidates=["10", "15"], reason="smudged")],
            ),
        ],
    )

    heading, paragraph = doc.blocks
    assert (heading.id, heading.level, paragraph.id) == ("b3.1", 2, "b3.2")
    assert paragraph.text == "Оплата не пізніше {d3.1} числа."
    assert paragraph.doubts[0].candidates == ["10", "15"] and paragraph.bbox is None
    assert "⟦1? | open | 10 · 15⟧" in render_markdown(doc)


def test_running_headers_and_page_numbers_are_dropped():
    pages = [
        _page(n, HEADER, para(f"Текст сторінки {n}."), para(f"стор. {n} з 3")) for n in (1, 2, 3)
    ]
    doc = assemble("Lease", "lease.pdf", pages)
    assert [b.text for b in doc.blocks] == [
        "Текст сторінки 1.",
        "Текст сторінки 2.",
        "Текст сторінки 3.",
    ]
    assert doc.pages[1].blocks == ["b2.2"]  # ids kept from cleanup's order


def test_a_word_hyphenated_across_a_page_break_is_joined_and_the_block_spans_both_pages():
    doc = assemble(
        "Lease",
        "lease.pdf",
        [
            _page(3, para("У разі прострочення платежу нараховується пе-")),
            _page(4, para("ня у розмірі 0,5%."), para("4. Строк дії.")),
        ],
    )

    joined = doc.blocks[0]
    assert joined.text == "У разі прострочення платежу нараховується пеня у розмірі 0,5%."
    assert (joined.id, joined.pages) == ("b3.1", [3, 4])
    assert doc.pages[1].blocks == ["b4.2", "b3.1"]
    assert "{#b3.1 p=3→4}" in render_markdown(doc)


def test_a_sentence_ending_on_a_page_isnt_joined_to_the_next():
    doc = assemble("L", "l.pdf", [_page(1, para("Кінець речення.")), _page(2, para("далі текст"))])
    assert len(doc.blocks) == 2


def test_a_table_continued_on_the_next_page_is_merged_unless_a_note_says_not_to():
    first = _page(3, table(["Період", "Сума"], ["березень", "250,00"]))
    second = _page(4, table(["Період", "Сума"], ["квітень", "250,00"]), para("Разом."))

    merged = assemble("L", "l.pdf", [first, second])
    assert merged.blocks[0].rows == [
        ["Період", "Сума"],
        ["березень", "250,00"],
        ["квітень", "250,00"],
    ]
    assert merged.blocks[0].pages == [3, 4]

    kept_apart = assemble(
        "L",
        "l.pdf",
        [first, _page(4, *second.cleaned.blocks, note="Don't merge this table with page 3.")],
    )
    assert [b.type for b in kept_apart.blocks] == ["table", "table", "paragraph"]


def test_an_unsure_ocr_word_left_in_the_text_becomes_a_doubt_after_cleanups_own():
    doc = assemble(
        "L",
        "l.pdf",
        [
            _page(
                2,
                para("Сума 250,00 грн, строк 1? днів."),
                table(["Сума"], ["250,00"]),
                doubts=[CleanDoubt(block=0, raw="1?", candidates=["10"])],
                unsure=["250,00", "Хрещатик"],
            ),
        ],
    )

    text, cells = doc.blocks
    assert text.text == "Сума {d2.2} грн, строк {d2.1} днів."
    assert [(d.id, d.source) for d in text.doubts] == [("d2.1", "clean"), ("d2.2", "ocr")]
    assert cells.rows[1] == ["{d2.3}"]


def test_an_unsure_ocr_word_cleanup_already_doubted_isnt_doubted_twice():
    doc = assemble(
        "L",
        "l.pdf",
        [_page(1, para("UNIT PRICE, UNIT COST"), doubts=[CleanDoubt(block=0, raw="UNIT")],
               unsure=["UNIT"])],
    )  # fmt: skip
    (block,) = doc.blocks
    assert block.text == "{d1.1} PRICE, UNIT COST"
    assert [d.source for d in block.doubts] == ["clean"]


def test_a_page_that_failed_cleanup_is_a_placeholder_and_the_rest_is_assembled():
    failed = PageInput(n=2, image="pages/p002.png", size=(10, 10), failed="the reply wasn't valid")
    doc = assemble("L", "l.pdf", [_page(1, para("Перша.")), failed, _page(3, para("Третя."))])

    assert [b.id for b in doc.blocks] == ["b1.1", "b3.1"]
    assert doc.pages[1].failed == "the reply wasn't valid"
    assert "[Page 2 wasn't cleaned: the reply wasn't valid]" in render_markdown(doc)


def test_another_page_cleaned_again_keeps_this_pages_ids():
    one = _page(1, para("Один {x}."), para("Два."), doubts=[CleanDoubt(block=1, raw="Два")])
    before = assemble("L", "l.pdf", [one, _page(2, para("Три."))])
    after = assemble(
        "L",
        "l.pdf",
        [one, _page(2, para("Нове."), para("Ще."), doubts=[CleanDoubt(block=0, raw="Нове")])],
    )

    assert [b.id for b in before.blocks[:2]] == [b.id for b in after.blocks[:2]] == ["b1.1", "b1.2"]
    assert before.blocks[1].doubts[0].id == after.blocks[1].doubts[0].id == "d1.1"


def test_two_pages_that_start_alike_keep_their_lines():
    doc = assemble("L", "l.pdf", [_page(n, HEADER, para(f"Текст {n}.")) for n in (1, 2)])
    assert [b.text for b in doc.blocks][0] == HEADER.text  # two pages aren't a running header


def test_a_long_repeated_paragraph_is_never_taken_for_a_running_line():
    long = para("Орендар зобов'язаний сплачувати орендну плату вчасно. " * 3)
    ends = {1: "Перший розділ.", 2: "Другий розділ.", 3: "Третій розділ."}
    doc = assemble("L", "l.pdf", [_page(n, long, para(ends[n])) for n in (1, 2, 3)])
    assert len(doc.blocks) == 6


def test_a_page_never_loses_its_only_block_to_a_running_line():
    doc = assemble("L", "l.pdf", [_page(n, para(f"Форма {n} заповнена.")) for n in (1, 2, 3)])
    assert [b.id for b in doc.blocks] == ["b1.1", "b2.1", "b3.1"]


def test_a_table_given_as_text_is_read_into_rows_and_still_merges_across_pages():
    first = _page(
        1, CleanBlock(type="table", text="Період        Сума, грн\nберезень 1998   250,00")
    )
    second = _page(
        2, CleanBlock(type="table", text="| Період | Сума, грн |\n|---|---|\n| Разом | 750,00 |")
    )

    (table,) = assemble("L", "l.pdf", [first, second]).blocks
    assert table.rows == [["Період", "Сума, грн"], ["березень 1998", "250,00"], ["Разом", "750,00"]]
    assert table.pages == [1, 2]


def test_a_page_number_the_model_called_a_signature_is_dropped_and_the_break_joins():
    doc = assemble(
        "L",
        "l.pdf",
        [
            _page(
                2,
                para("пеню у розмірі 0,5% від суми заборго-"),
                CleanBlock(type="signature", text="стор. 2 з 3"),
            ),
            _page(
                3, CleanBlock(type="stamp", text="- 3 -"), para("ваності, включно з днем оплати.")
            ),
        ],
    )
    (block,) = doc.blocks
    assert block.text == "пеню у розмірі 0,5% від суми заборгованості, включно з днем оплати."
    assert block.pages == [2, 3]
