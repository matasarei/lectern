from __future__ import annotations

import pytest

from lectern.documents.document import render_markdown
from lectern.modules.ocr.assemble import PageInput, assemble
from lectern.modules.ocr.clean import CleanBlock, CleanDoubt, CleanedPage
from lectern.modules.ocr.review import Decision, apply_decisions


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


def heading(text: str) -> CleanBlock:
    return CleanBlock(type="heading", level=2, text=text)


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


def test_a_running_header_merged_into_a_heading_is_taken_off_it():
    """M4 pages 4, 7 and 10: cleanup put the header and the section's heading in one block."""
    header = para("Договір оренди № 17/2026")
    pages = [_page(n, header, para(f"Текст сторінки {n}.")) for n in (1, 2, 3)]
    pages.append(_page(4, para("Договір оренди № 17/2026\n4. Розділ 4"), para("Текст сторінки 4.")))
    # The header's words as a page's last line: a header, not a footer, so they stay there.
    pages.append(_page(5, para("Підписи сторін\nДоговір оренди № 17/2026")))
    doc = assemble("Lease", "lease.pdf", pages)
    texts = [b.text for b in doc.blocks]
    assert "4. Розділ 4" in texts and "Підписи сторін\nДоговір оренди № 17/2026" in texts
    assert not any(t.startswith("Договір оренди") for t in texts)


def test_sections_numbered_in_turn_at_the_top_of_pages_stay():
    """On main since before the M4 re-run: "4. Розділ 4", "5. Розділ 5" and "6. Розділ 6" are
    alike once digits are set aside, and went as a running header."""
    pages = [_page(n, heading(f"{n}. Розділ {n}"), para(f"Текст {n}.")) for n in (4, 5, 6, 7)]
    texts = [b.text for b in assemble("Lease", "lease.pdf", pages).blocks]
    assert [t for t in texts if "Розділ" in t] == [f"{n}. Розділ {n}" for n in (4, 5, 6, 7)]


def test_a_numbered_heading_with_more_lines_keeps_its_first():
    pages = [_page(n, heading(f"{n}. Розділ {n}"), para(f"Текст {n}.")) for n in (1, 2, 3)]
    pages.append(_page(4, heading("4. Розділ 4\nОплата"), para("Текст 4.")))
    texts = [b.text for b in assemble("Lease", "lease.pdf", pages).blocks]
    assert "4. Розділ 4\nОплата" in texts


def test_a_header_merged_into_numbered_headings_goes_and_the_headings_stay():
    """Both at once, as the lease's pages have them: the header's line is a running line, and
    the numbered line under it isn't."""
    header = para("Договір оренди № 17/2026")
    pages = [_page(n, header, heading(f"{n}. Розділ {n}"), para(f"Текст {n}.")) for n in (1, 2, 3)]
    pages += [
        _page(n, heading(f"Договір оренди № 17/2026\n{n}. Розділ {n}"), para(f"Текст {n}."))
        for n in (4, 5, 6)
    ]
    texts = [b.text for b in assemble("Lease", "lease.pdf", pages).blocks]
    assert [t for t in texts if "Розділ" in t] == [f"{n}. Розділ {n}" for n in range(1, 7)]
    assert not any("Договір оренди" in t for t in texts)


def test_a_numbered_heading_typed_as_a_paragraph_on_some_pages_stays():
    """Cleanup doesn't type a page's heading the same way every time (the M4 re-run did both).
    A leftover line can be removed in the review; a lost one can't be noticed."""
    pages = [_page(n, heading(f"{n}. Розділ {n}"), para(f"Текст {n}.")) for n in (1, 2)]
    pages += [_page(n, para(f"{n}. Розділ {n}"), para(f"Текст {n}.")) for n in (3, 4)]
    texts = [b.text for b in assemble("Lease", "lease.pdf", pages).blocks]
    assert [t for t in texts if "Розділ" in t] == [f"{n}. Розділ {n}" for n in (1, 2, 3, 4)]


def test_a_heading_repeated_word_for_word_is_still_a_running_header():
    pages = [_page(n, heading("Договір оренди № 17/2026"), para(f"Текст {n}.")) for n in (1, 2, 3)]
    texts = [b.text for b in assemble("Lease", "lease.pdf", pages).blocks]
    assert texts == ["Текст 1.", "Текст 2.", "Текст 3."]


def test_a_paragraph_numbered_page_by_page_is_still_a_running_line():
    pages = [_page(n, para(f"Договір, аркуш {n}"), para(f"Текст {n}.")) for n in (1, 2, 3)]
    texts = [b.text for b in assemble("Lease", "lease.pdf", pages).blocks]
    assert texts == ["Текст 1.", "Текст 2.", "Текст 3."]


def test_a_merged_header_and_heading_alike_on_many_pages_loses_only_the_header():
    """The M4 re-run: the header and the section's heading came back as one block on pages 4,
    7, 10 and 11, alike once digits are set aside, so the block itself looked like a running
    line and went whole, heading and all."""
    header = para("Договір оренди № 17/2026")
    pages = [_page(n, header, para(f"Текст сторінки {n}.")) for n in (1, 2, 3)]
    pages += [
        _page(n, para(f"Договір оренди № 17/2026\n{n}. Розділі {n}"), para(f"Текст {n}."))
        for n in (4, 7, 10, 11)
    ]
    texts = [b.text for b in assemble("Lease", "lease.pdf", pages).blocks]
    assert [t for t in texts if "Розділі" in t] == [f"{n}. Розділі {n}" for n in (4, 7, 10, 11)]
    assert not any("Договір оренди" in t for t in texts)


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


def test_a_question_in_a_table_given_as_text_and_rows_is_marked_in_its_cell():
    """Seen on a real invoice: cleanup gave the table both as text and as rows. The table is
    shown from its rows, so a question marked only in its text would vanish."""
    both = CleanBlock(type="table", text="Rate\n100.00", rows=[["Rate"], ["100.00"]])
    doc = assemble("L", "l.pdf", [_page(5, both, doubts=[CleanDoubt(block=0, raw="100.00")])])

    (block,) = doc.blocks
    assert block.rows == [["Rate"], ["{d5.1}"]]
    assert "| ⟦100.00 \\| open⟧ |" in render_markdown(doc)  # in its cell, | escaped


def _amid(n: int, *blocks: CleanBlock, **kw) -> PageInput:  # noqa: ANN003
    """A page's blocks between text of its own: not at an edge, where a repeat is a header."""
    return _page(n, para(f"Початок {'а' * n}."), *blocks, para(f"Кінець {'б' * n}."), **kw)


def _lines(*texts: str) -> list[PageInput]:
    return [_amid(n, para(text)) for n, text in enumerate(texts, start=1)]


def _asked(doc) -> list[str]:  # noqa: ANN001
    return [d.raw for b in doc.blocks for d in b.doubts]


MISREAD_ON = {3, 4, 7, 10, 11}


def _lease() -> list[PageInput]:
    """The M4 lease's headings: "Розділ" on the scan, "Розділі" as read on five pages (#158)."""
    words = {n: "Розділі" if n in MISREAD_ON else "Розділ" for n in range(1, 13)}
    return [_amid(n, heading(f"{n}. {word} {n}")) for n, word in words.items()]


def test_a_repeated_line_one_letter_off_the_commoner_ones_is_questioned():
    doc = assemble("L", "l.pdf", _lease())

    doubted = {b.page: b for b in doc.blocks if b.doubts}
    assert set(doubted) == MISREAD_ON
    block = doubted[3]
    assert block.text == "3. {d3.1} 3"
    doubt = block.doubts[0]
    assert (doubt.raw, doubt.candidates, doubt.source) == ("Розділі", ["Розділ"], "repeat")
    assert "Розділ" in doubt.reason


def test_accepting_a_repeated_lines_question_changes_that_line_only():
    doc = assemble("L", "l.pdf", _lease())
    answer = Decision(page=3, doubt_id="d3.1", raw="Розділі", value="Розділ", action="accept")

    answered, unplaced = apply_decisions(doc, [answer])

    assert unplaced == []
    assert {d.id: d.status for b in answered.blocks for d in b.doubts} == {
        "d3.1": "resolved", "d4.1": "open", "d7.1": "open", "d10.1": "open", "d11.1": "open",
    }  # fmt: skip
    text = render_markdown(answered)
    assert "3. Розділ 3" in text
    assert "3. Розділі 3" not in text
    assert "⟦Розділі | open | Розділ⟧ 4" in text


def test_an_answer_stays_when_the_pages_other_questions_move():
    """Cleaned again with a question of its own, d3.1 is that one: the answer follows the text."""
    answer = Decision(page=3, doubt_id="d3.1", raw="Розділі", value="Розділ", action="accept")
    pages = _lease()
    pages[2] = _amid(3, para("Сума 1? грн."), heading("3. Розділі 3"),
                     doubts=[CleanDoubt(block=1, raw="1?", candidates=["10"])])  # fmt: skip

    answered, unplaced = apply_decisions(assemble("L", "l.pdf", pages), [answer])

    assert unplaced == []
    by_raw = {d.raw: d for b in answered.blocks for d in b.doubts if b.page == 3}
    assert (by_raw["Розділі"].id, by_raw["Розділі"].status) == ("d3.2", "resolved")
    assert by_raw["1?"].status == "open"


@pytest.mark.parametrize(
    "pages",
    [
        pytest.param(_lines("1. Розділі 1", "2. Розділ 2", "3. Розділ 3"), id="commoner-under-3"),
        pytest.param(
            _lines("1. Розділі 1", "2. Розділі 2", "3. Розділ 3", "4. Розділ 4"), id="a-tie"
        ),
        pytest.param(
            _lines("1. розділ 1", "2. Розділ 2", "3. Розділ 3", "4. Розділ 4"), id="case-only"
        ),
        pytest.param(_lines("1. Том 1", "2. Тім 2", "3. Тім 3", "4. Тім 4"), id="short-word"),
        pytest.param(
            _lines("Розділі один", "Розділ два", "Розділ два", "Розділ два"), id="no-digit"
        ),
        pytest.param(
            _lines(
                "1. Глава Розділі 1",
                "2. Частина Розділ 2",
                "3. Частина Розділ 3",
                "4. Частина Розділ 4",
            ),  # fmt: skip
            id="two-words-differ",
        ),
        pytest.param(
            _lines("1. Розділили 1", "2. Розділ 2", "3. Розділ 3", "4. Розділ 4"), id="two-letters"
        ),
        pytest.param(
            _lines("1. Розділі1", "2. Розділ2", "3. Розділ3", "4. Розділ4"), id="digits-on-the-word"
        ),
        pytest.param(
            [
                _amid(n, para(f"{n}. Розділ {n} " + "і довгий текст далі " * 3 + word))
                for n, word in enumerate(["а", "а", "а", "а"], start=1)
            ]
            + [_amid(5, para("5. Розділі 5 " + "і довгий текст далі " * 3 + "а"))],
            id="a-long-line",
        ),  # fmt: skip
        pytest.param(
            [
                _amid(1, table(["1", "Розділі"])),
                _amid(2, table(["2", "Розділ"])),
                _amid(3, table(["3", "Розділ"])),
                _amid(4, table(["4", "Розділ"])),
            ],
            id="a-table",
        ),  # fmt: skip
    ],
)
def test_lines_that_arent_a_one_letter_repeat_ask_nothing(pages: list[PageInput]):
    assert _asked(assemble("L", "l.pdf", pages)) == []


def test_a_word_on_an_already_questioned_line_isnt_asked_again():
    pages = _lease()
    pages[2] = _amid(3, heading("3. Розділі 3"),
                     doubts=[CleanDoubt(block=1, raw="Розділі", candidates=["Розділ"])])  # fmt: skip

    doc = assemble("L", "l.pdf", pages)

    on_page_3 = [d for b in doc.blocks if b.page == 3 for d in b.doubts]
    assert [(d.id, d.source) for d in on_page_3] == [("d3.1", "clean")]


def test_the_question_goes_on_that_line_though_the_block_has_the_word_earlier():
    pages = _lease()
    pages[2] = _amid(3, para("Розділі цього договору діють.\n3. Розділі 3"))

    doc = assemble("L", "l.pdf", pages)

    block = next(b for b in doc.blocks if b.page == 3 and b.doubts)
    assert block.text == "Розділі цього договору діють.\n3. {d3.1} 3"


def test_earlier_doubts_on_the_page_keep_their_ids():
    pages = _lease()
    pages[2] = _amid(3, para("Сума 1? грн, Хрещатик."), heading("3. Розділі 3"),
                     doubts=[CleanDoubt(block=1, raw="1?", candidates=["10"])],
                     unsure=["Хрещатик"])  # fmt: skip

    doc = assemble("L", "l.pdf", pages)

    on_page_3 = {d.id: d.source for b in doc.blocks if b.page == 3 for d in b.doubts}
    assert on_page_3 == {"d3.1": "clean", "d3.2": "ocr", "d3.3": "repeat"}
