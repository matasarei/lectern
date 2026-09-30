"""An OCR session's files and rows.

The output folder sits in the project, named after the source file, as the blueprint lays it
out: `<Title>/pages/p001.png`, `p001.ocr.md` (the page's current text) and one
`p001.<model_id>.ocr.md` per model that read it, so readings can be compared. The token
log-probabilities go beside them in `p001.<model_id>.json`, for M3's doubt spans. These files
belong to the session: they aren't listed as project files.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from lectern.modules.ocr.assemble import PageInput
from lectern.modules.ocr.clean import CleanedPage
from lectern.modules.ocr.document import Document, render_markdown
from lectern.modules.ocr.doubts import unsure_words
from lectern.modules.ocr.pages import IMAGE_SUFFIXES, PageImage, page_file
from lectern.modules.ocr.recognise import PageText
from lectern.modules.ocr.text import looped
from lectern.projects import files, search
from lectern.projects.tables import ArtifactRow, OcrPageRow, OcrSessionRow, SessionRow, now
from lectern.projects.text import extract

DOCUMENT_JSON = "document.json"
DOCUMENT_MD = "document.md"

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
        page.reading += 1  # a new reading: its cleanup (M3a) is due again
        page.clean_status = "pending"
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


# --- cleanup and the Document (M3a) ---------------------------------------------------------


def page_tokens(root: Path, ocr: OcrSessionRow, page: OcrPageRow) -> list[tuple[str, float]] | None:
    """The token log-probabilities of the page's current reading, if the runner gave them."""
    path = pages_dir(root, ocr) / f"{page.key}.{page.model_id}.json"
    if not page.model_id or not path.is_file():
        return None
    tokens = json.loads(path.read_text(encoding="utf-8")).get("tokens")
    return [(t, float(p)) for t, p in tokens] if tokens else None


def mark_cleaning(db: sessionmaker[Session], page_id: int, status: str) -> OcrPageRow:
    with db.begin() as session:
        page = session.get(OcrPageRow, page_id)
        page.clean_status = status
        return page


def save_cleanup(
    root: Path, db: sessionmaker[Session], ocr: OcrSessionRow, page_id: int, cleaned: CleanedPage
) -> OcrPageRow:
    """Keep the page's cleanup (p001.clean.json) and record the reading it came from."""
    with db.begin() as session:
        page = session.get(OcrPageRow, page_id)
        path = pages_dir(root, ocr) / f"{page.key}.clean.json"
        path.write_text(cleaned.model_dump_json(indent=1), encoding="utf-8")
        page.clean_status, page.clean_error = "done", None
        page.cleaned_reading = page.reading
        page.updated_at = now()
        return page


def cleanup_failed(db: sessionmaker[Session], page_id: int, error: str) -> OcrPageRow:
    with db.begin() as session:
        page = session.get(OcrPageRow, page_id)
        page.clean_status, page.clean_error = "failed", error
        page.cleaned_reading = page.reading
        page.updated_at = now()
        return page


def assembly_inputs(root: Path, db: sessionmaker[Session], ocr: OcrSessionRow) -> list[PageInput]:
    """Every page as assembly needs it: its cleanup, or why there's none."""
    folder = pages_dir(root, ocr)
    inputs = []
    for page in pages(db, ocr.session_id):
        cleaned, failed = None, None
        clean_file = folder / f"{page.key}.clean.json"
        if page.status != "done":
            failed = page.error or "the page wasn't read"
        elif page.clean_status == "done" and clean_file.is_file():
            cleaned = CleanedPage.model_validate_json(clean_file.read_text(encoding="utf-8"))
        else:
            failed = page.clean_error or "the page wasn't cleaned"
        inputs.append(
            PageInput(
                n=page.number,
                image=f"{PAGES_DIR}/{page_file(page.number)}",
                size=(page.width, page.height),
                note=page.note,
                cleaned=cleaned,
                failed=failed,
                unsure=unsure_words(page_tokens(root, ocr, page)),
            )
        )
    return inputs


def publish_document(
    root: Path, db: sessionmaker[Session], session_id: int, owner_id: int, document: Document
) -> OcrSessionRow:
    """Write document.json and document.md into the session's folder. document.md is a project
    file from the session (Overview and the chat's search find it): created the first time,
    and its contents replaced and indexed again for each new version."""
    with db.begin() as session:
        ocr = session.get(OcrSessionRow, session_id)
        folder = files.inside(root, ocr.folder)
        markdown = render_markdown(document)
        (folder / DOCUMENT_JSON).write_text(document.to_json(), encoding="utf-8")
        row = (
            session.get(ArtifactRow, ocr.document_artifact_id) if ocr.document_artifact_id else None
        )
        if row is None or row.trashed_at is not None:
            row = files.write_text(
                root, session, owner_id=owner_id, name=DOCUMENT_MD, text=markdown,
                origin=files.Origin("session", session_id), folder=ocr.folder,
            )  # fmt: skip
        else:
            path = files.inside(root, row.path)
            data = markdown.encode("utf-8")
            path.write_bytes(data)
            row.size, row.sha256 = len(data), hashlib.sha256(data).hexdigest()
            row.modified_at = now()
            extracted = extract(path)
            row.text_status = "indexed" if extracted.has_text else "none"
            search.index(session, row.id, extracted.chunks)
        ocr.document_version = document.version
        ocr.document_artifact_id = row.id
        ocr.updated_at = now()
        return ocr


def load_document(root: Path, ocr: OcrSessionRow) -> Document | None:
    path = files.inside(root, ocr.folder) / DOCUMENT_JSON
    return Document.from_json(path.read_text(encoding="utf-8")) if path.is_file() else None
