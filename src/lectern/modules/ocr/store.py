"""An OCR session's files and rows.

The output folder sits in the project, named after the source file, as the blueprint lays it
out: `<Title>/pages/p001.png`, `p001.ocr.md` (the page's current text) and one
`p001.<model_id>.ocr.md` per model that read it, so readings can be compared. The token
log-probabilities go beside them in `p001.<model_id>.json`, for M3's doubt spans. These files
belong to the session: they aren't listed as project files.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from lectern.modules.ocr.pages import IMAGE_SUFFIXES, PageImage, page_file
from lectern.modules.ocr.recognise import PageText
from lectern.modules.ocr.text import looped
from lectern.projects import files
from lectern.projects.tables import ArtifactRow, OcrPageRow, OcrSessionRow, SessionRow, now

PAGES_DIR = "pages"


class CannotOpen(Exception):
    """This file can't become an OCR session. The message says why, for the person."""


def create_session(
    root: Path, db: sessionmaker[Session], *, owner_id: int, artifact_id: int
) -> tuple[SessionRow, OcrSessionRow]:
    """A new OCR session for a PDF or image in the project, with its output folder reserved.
    The job fills it."""
    with db.begin() as session:
        source = session.get(ArtifactRow, artifact_id)
        if source is None or source.owner_id != owner_id or source.trashed_at is not None:
            raise CannotOpen("That file isn't in the project any more.")
        name = source.path.rsplit("/", 1)[-1]
        suffix = Path(name).suffix.lower()
        if suffix != ".pdf" and suffix not in IMAGE_SUFFIXES:
            raise CannotOpen(f"{name} isn't a PDF or an image, so it can't be read as pages.")
        folder = files.free_name(root, Path(name).stem or "OCR")
        (root / folder / PAGES_DIR).mkdir(parents=True)
        row = SessionRow(owner_id=owner_id, kind="ocr", title=name, titled=True)
        session.add(row)
        session.flush()
        ocr = OcrSessionRow(session_id=row.id, source_artifact_id=source.id, folder=folder)
        session.add(ocr)
    return row, ocr


def pages_dir(root: Path, ocr: OcrSessionRow) -> Path:
    return files.inside(root, f"{ocr.folder}/{PAGES_DIR}")


def source_path(root: Path, db: sessionmaker[Session], ocr: OcrSessionRow) -> Path:
    with db() as session:
        source = session.get(ArtifactRow, ocr.source_artifact_id)
    if source is None or source.trashed_at is not None:
        raise CannotOpen("The file this session reads was moved to the trash.")
    return files.inside(root, source.path)


def add_pages(db: sessionmaker[Session], session_id: int, images: list[PageImage]) -> None:
    """One row per page image `rasterise` wrote into the session's pages folder."""
    with db.begin() as session:
        for image in images:
            session.add(
                OcrPageRow(
                    session_id=session_id,
                    number=image.number,
                    key=Path(page_file(image.number)).stem,
                    width=image.width,
                    height=image.height,
                    has_hint=image.text_hint is not None,
                )
            )
        ocr = session.get(OcrSessionRow, session_id)
        ocr.pages_total = len(images)
        ocr.updated_at = now()


def pages(db: sessionmaker[Session], session_id: int) -> list[OcrPageRow]:
    with db() as session:
        return list(
            session.scalars(
                select(OcrPageRow)
                .where(OcrPageRow.session_id == session_id)
                .order_by(OcrPageRow.number)
            )
        )


def save_reading(
    root: Path,
    db: sessionmaker[Session],
    ocr: OcrSessionRow,
    page_id: int,
    *,
    model_id: str,
    reading: PageText,
) -> OcrPageRow:
    """Write a model's reading of a page, make it the page's current text, and record it."""
    folder = pages_dir(root, ocr)
    with db.begin() as session:
        page = session.get(OcrPageRow, page_id)
        (folder / f"{page.key}.{model_id}.ocr.md").write_text(reading.text + "\n", encoding="utf-8")
        (folder / f"{page.key}.ocr.md").write_text(reading.text + "\n", encoding="utf-8")
        detail = json.dumps(asdict(reading) | {"text": None}, ensure_ascii=False)
        (folder / f"{page.key}.{model_id}.json").write_text(detail, encoding="utf-8")
        page.status = "done"
        page.model_id = model_id
        page.seconds = reading.seconds
        page.cut_off = reading.cut_off
        page.looped = looped(reading.text)
        page.error = None
        page.updated_at = now()
        return page


def page_failed(db: sessionmaker[Session], page_id: int, error: str) -> OcrPageRow:
    with db.begin() as session:
        page = session.get(OcrPageRow, page_id)
        page.status = "failed"
        page.error = error
        page.updated_at = now()
        return page


def page_image(root: Path, ocr: OcrSessionRow, page: OcrPageRow) -> Path:
    return pages_dir(root, ocr) / page_file(page.number)


def page_text(root: Path, ocr: OcrSessionRow, page: OcrPageRow) -> str | None:
    """The page's current text, or None before it has been read."""
    path = pages_dir(root, ocr) / f"{page.key}.ocr.md"
    return path.read_text(encoding="utf-8").removesuffix("\n") if path.is_file() else None
