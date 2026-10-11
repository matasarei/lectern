from __future__ import annotations

from lectern.modules.ocr.text import cut_trailing_loop, looped


def test_an_output_that_goes_round_again_is_flagged():
    page = "# Channel Tone Ladder\nRed\nGreen\nBlue\n# Uniformity Bands\nDark\nMid\nLight\nEnd"
    assert looped(page + "\n" + page)
    assert not looped(page)
    rows = "<tr>\n<td>Plan</td>\n<td>100</td>\n<td>1</td>\n<td>$100</td>\n</tr>\n"
    table = rows + rows.replace("Plan", "Reporting")
    assert not looped(table)  # two rows with the same values are not a loop


HEAD = "Channel Tone Ladder\nRed\nGreen\nBlue\n\nTone Gradient Strips\nRed\nDark\n4"


def test_a_line_the_model_got_stuck_on_is_cut_to_its_first_time():
    # As PaddleOCR-VL read a scanner test page: "Red" until it ran out of room.
    text, repeated, more = cut_trailing_loop(HEAD + "\nRed" * 3451)
    assert text == HEAD + "\nRed"  # the "Red" lines before the loop are the page's own
    assert (repeated, more) == ("Red", 3450)


def test_a_run_of_lines_said_over_and_over_is_cut_even_when_the_last_round_is_cut_short():
    block = "Item\nQty\nTotal\n100\n1\n$100"
    text, repeated, more = cut_trailing_loop("Invoice 7\n" + "\n".join([block] * 6) + "\nItem\nQt")
    assert text == "Invoice 7\n" + block
    assert repeated == block
    assert more == 6  # five whole rounds more, and the one cut short


def test_lines_a_page_really_repeats_are_left_alone():
    signatures = "Signed:\n" + "________\n" * 6  # six blank signature lines
    assert cut_trailing_loop(signatures) == (signatures, "", 0)
    twice = HEAD + "\n" + HEAD  # the page read twice is flagged as looped, not cut here
    assert cut_trailing_loop(twice) == (twice, "", 0)
    assert cut_trailing_loop("") == ("", "", 0)


def test_blank_lines_at_the_end_arent_a_loop():
    padded = HEAD + "\n" * 40
    assert cut_trailing_loop(padded) == (padded, "", 0)
