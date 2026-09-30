from __future__ import annotations

from lectern.modules.ocr.text import looped


def test_an_output_that_goes_round_again_is_flagged():
    page = "# Channel Tone Ladder\nRed\nGreen\nBlue\n# Uniformity Bands\nDark\nMid\nLight\nEnd"
    assert looped(page + "\n" + page)
    assert not looped(page)
    rows = "<tr>\n<td>Plan</td>\n<td>100</td>\n<td>1</td>\n<td>$100</td>\n</tr>\n"
    table = rows + rows.replace("Plan", "Reporting")
    assert not looped(table)  # two rows with the same values are not a loop
