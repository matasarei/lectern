"""Reviewing a Document: the person's decisions on its doubts (docs/ocr.md, "Review").

A decision is kept apart from document.json (in `ocr_decisions`), because the Document is
assembled again from the page cleanups each time a page is cleaned again. Every assembly
applies the decisions, so a version never loses one. A decision names its doubt by page, id
and the text it was about. When cleaning the page again renumbered the doubts, it follows its
text; when that text is gone from the page, it applies nowhere and is reported as unplaced,
never moved to some other doubt.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from lectern.documents.document import Document, Doubt, covers_block

RESOLVED_BY = "person"
DOUBT_PAGE = re.compile(r"^d(\d+)\.")


@dataclass(frozen=True)
class Decision:
    page: int
    doubt_id: str  # "d3.1", as it was when decided
    raw: str  # the text the doubt was about
    value: str  # the reading chosen; "" when the block was removed
    action: str  # "accept" | "edit" | "keep" | "remove"
    id: int | None = None  # its row in ocr_decisions


def doubt_page(doubt: Doubt) -> int | None:
    """The page a doubt came from: its id says so (d3.1), whatever block it ended up in."""
    match = DOUBT_PAGE.match(doubt.id)
    return int(match.group(1)) if match else None


def match_decisions(
    document: Document, decisions: list[Decision]
) -> tuple[dict[str, Decision], list[Decision]]:
    """Which decision each doubt takes (by doubt id), and the decisions that match none.
    First by page, id and text; what's left by page and text, taking the first doubt on that
    text that no decision has taken yet. A removal matches only a doubt that is still all of
    its block: cleaned again into a longer one, its "" would delete a word from real text."""
    covering = {d.id for b in document.blocks for d in b.doubts if covers_block(b, d.id)}
    doubts = [d for b in document.blocks for d in b.doubts]

    def fits(decision: Decision, doubt: Doubt) -> bool:
        return decision.action != "remove" or doubt.id in covering

    matched: dict[str, Decision] = {}
    left: list[Decision] = []
    for decision in decisions:
        same = next(
            (
                d
                for d in doubts
                if d.id == decision.doubt_id
                and d.raw == decision.raw
                and d.id not in matched
                and fits(decision, d)
            ),
            None,
        )
        if same is not None:
            matched[same.id] = decision
        else:
            left.append(decision)
    unplaced: list[Decision] = []
    for decision in left:
        moved = next(
            (
                d
                for d in doubts
                if d.id not in matched
                and d.raw == decision.raw
                and doubt_page(d) == decision.page
                and fits(decision, d)
            ),
            None,
        )
        if moved is not None:
            matched[moved.id] = decision
        else:
            unplaced.append(decision)
    return matched, unplaced


def apply_decisions(
    document: Document, decisions: list[Decision]
) -> tuple[Document, list[Decision]]:
    """A copy of the Document with each decided doubt resolved (and every other one open),
    and the decisions that matched no doubt."""
    doc = document.model_copy(deep=True)
    matched, unplaced = match_decisions(doc, decisions)
    for block in doc.blocks:
        for doubt in block.doubts:
            decision = matched.get(doubt.id)
            if decision is None:
                doubt.status, doubt.value, doubt.resolved_by = "open", None, None
            else:
                doubt.status, doubt.value = "resolved", decision.value
                doubt.resolved_by = RESOLVED_BY
    return doc, unplaced
