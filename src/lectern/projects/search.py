"""Full-text search over a project's files (SQLite FTS5, table `artifact_text`).

A query is split into words, each matched as a word prefix, all of them required. The
person's text never reaches FTS5's query syntax, so quotes, `*`, `NEAR` or `-` in a question
are just characters.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from lectern.projects.text import Chunk


@dataclass(frozen=True)
class Hit:
    artifact_id: int
    chunk: int
    heading: str
    snippet: str
    label: str | None = None  # a Document's block id

    @property
    def ref(self) -> str:
        return f"#{self.label}" if self.label else f"#c{self.chunk}"


def index(session: Session, artifact_id: int, chunks: list[Chunk]) -> None:
    remove(session, artifact_id)
    for chunk in chunks:
        session.execute(
            text(
                "INSERT INTO artifact_text (artifact_id, chunk, heading, body) "
                "VALUES (:a, :c, :h, :b)"
            ),
            {"a": artifact_id, "c": chunk.number, "h": chunk.heading, "b": chunk.body},
        )
        if chunk.label:
            session.execute(
                text(
                    "INSERT INTO artifact_chunk_labels (artifact_id, chunk, label) "
                    "VALUES (:a, :c, :l)"
                ),
                {"a": artifact_id, "c": chunk.number, "l": chunk.label},
            )


def remove(session: Session, artifact_id: int) -> None:
    session.execute(text("DELETE FROM artifact_text WHERE artifact_id = :a"), {"a": artifact_id})
    session.execute(
        text("DELETE FROM artifact_chunk_labels WHERE artifact_id = :a"), {"a": artifact_id}
    )


def chunks(session: Session, artifact_id: int) -> list[Chunk]:
    rows = session.execute(
        text(
            "SELECT t.chunk, t.heading, t.body, l.label FROM artifact_text t "
            "LEFT JOIN artifact_chunk_labels l "
            "ON l.artifact_id = t.artifact_id AND l.chunk = t.chunk "
            "WHERE t.artifact_id = :a ORDER BY t.chunk"
        ),
        {"a": artifact_id},
    )
    return [Chunk(int(r.chunk), r.heading or "", r.body or "", r.label) for r in rows]


def match_expression(query: str) -> str | None:
    words = re.findall(r"\w+", query)
    if not words:
        return None
    return " ".join(f'"{w}"*' for w in words[:12])


SEARCH_SQL = (
    # A chunk's label, if it has one (a Document's block id), comes from the table beside it.
    "SELECT artifact_id, chunk, heading, "
    "(SELECT label FROM artifact_chunk_labels l WHERE l.artifact_id = artifact_text.artifact_id"
    " AND l.chunk = artifact_text.chunk) AS label, "
    "snippet(artifact_text, 3, '', '', '…', 16) AS snip "
    "FROM artifact_text WHERE artifact_text MATCH :q {only}"
    "ORDER BY bm25(artifact_text) LIMIT :limit"
)


def search(
    session: Session, query: str, *, limit: int = 10, artifact_ids: list[int] | None = None
) -> list[Hit]:
    expression = match_expression(query)
    if expression is None:
        return []
    params: dict = {"q": expression, "limit": limit}
    if artifact_ids is None:
        statement = text(SEARCH_SQL.format(only=""))
    elif not artifact_ids:
        return []
    else:
        statement = text(SEARCH_SQL.format(only="AND artifact_id IN :ids ")).bindparams(
            bindparam("ids", expanding=True)
        )
        params["ids"] = [int(a) for a in artifact_ids]
    rows = session.execute(statement, params)
    return [
        Hit(int(r.artifact_id), int(r.chunk), r.heading or "", r.snip or "", r.label) for r in rows
    ]
