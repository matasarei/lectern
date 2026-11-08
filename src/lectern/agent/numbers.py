"""Numbers in text, read the ways people write them. The chat loop uses it to tell a number
a tool would write from one the person or a tool result gave; agent-check, to score answers."""

from __future__ import annotations

import re

# A comma, dot or apostrophe joins digits only when digits follow at once ("4,172.04");
# a space joins only a group of exactly three digits ("4 172,04"). So "300, 500 and 200"
# stays three numbers.
_NUMBER = re.compile("-?\\d+(?:[.,']\\d+|[ \u00a0\u202f]\\d{3}(?!\\d))*")


def numbers_in(text: str) -> list[float]:
    """Every number in `text`, read the ways people write them: 4,172.04 · 4 172,04 ·
    4172.04 · 62,50. A lone comma before exactly three digits is a thousands separator."""
    return [value for _, value in written_numbers(text)]


def written_numbers(text: str) -> list[tuple[str, float]]:
    """Each number in `text` as written, and its value (as numbers_in reads it)."""
    found: list[tuple[str, float]] = []
    for raw in _NUMBER.findall(text):
        token = re.sub("[\u00a0\u202f ']", "", raw)
        if "," in token and "." in token:
            decimal = "," if token.rfind(",") > token.rfind(".") else "."
            thousands = "." if decimal == "," else ","
            token = token.replace(thousands, "").replace(decimal, ".")
        elif "," in token:
            parts = token.split(",")
            if len(parts) > 2 or (len(parts) == 2 and len(parts[1]) == 3):
                token = token.replace(",", "")
            else:
                token = token.replace(",", ".")
        elif token.count(".") > 1:
            token = token.replace(".", "")
        try:
            found.append((raw, float(token)))
        except ValueError:
            continue
    return found
