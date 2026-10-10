"""/api/projects/{id}/ocr: OCR sessions — start one on a PDF or image, follow its job, read
its pages, write page notes, read or clean a page again, pause and resume, and answer the
Document's doubts.

Starting returns at once (201); the job runs on the server and the tab follows it on the
session's events stream, which starts with the events so far.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from dataclasses import asdict
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from lectern.api.projects import CurrentUser, open_project, store
from lectern.api.sessions import open_session
from lectern.api.sse import event_response, pump
from lectern.catalog import catalog
from lectern.core.auth import User
from lectern.documents.document import render_page
from lectern.modules.ocr import store as ocr_store
from lectern.modules.ocr.jobs import NotNow, OcrJobs, page_dict
from lectern.modules.ocr.review import match_decisions
from lectern.projects.store import Project
from lectern.projects.tables import OcrPageRow, OcrSessionRow, SessionRow, iso, now
from lectern.runtime.wiring import cleanup_model

router = APIRouter()
log = logging.getLogger(__name__)
NOTE_LIMIT = 5000
VALUE_LIMIT = 200  # a reading: a word, a number, a short phrase


def jobs(request: Request) -> OcrJobs:
    return request.app.state.ocr_jobs


class NewOcr(BaseModel):
    artifact_id: int


class PageChange(BaseModel):
    note: str | None = Field(default=None, max_length=NOTE_LIMIT)
    handwritten: bool | None = None


class Answer(BaseModel):
    value: str = Field(min_length=1, max_length=VALUE_LIMIT)
    action: Literal["accept", "edit"]


class Rerun(BaseModel):
    model_id: str | None = None  # a catalog OCR model; none means the page's usual model


def _lookup(request: Request, project_id: int, session_id: int, user: User) -> Project:
    project = open_project(request, project_id, user)
    row = open_session(request, project, session_id, user)
    if row.kind != "ocr":
        raise HTTPException(404, "No such OCR session.")
    return project


async def open_ocr(request: Request, project_id: int, session_id: int, user: User) -> Project:
    """The project and OCR session lookups, in a worker thread: they read SQLite."""
    return await run_in_threadpool(_lookup, request, project_id, session_id, user)


def _session_dict(request: Request, project: Project, session_id: int) -> dict:
    # Reading can write: a job the app no longer runs (it was restarted mid-batch) is marked
    # paused here, the first time anyone looks. Nothing else in a read changes the session.
    ocr = jobs(request).settle(project, session_id)
    db = store(request).db(project)
    with db() as session:
        title = session.get(SessionRow, session_id).title
    return {
        "id": session_id,
        "kind": "ocr",
        "title": title,
        "folder": ocr.folder,
        "status": ocr.status,
        "paused_reason": ocr.paused_reason,
        "error": ocr.error,
        "pages_total": ocr.pages_total,
        "source_artifact_id": ocr.source_artifact_id,
        "document": _document_summary(request, project, ocr),
        "updated_at": iso(ocr.updated_at),
        "pages": [page_dict(p) for p in ocr_store.pages(db, session_id)],
    }


def _document_summary(request: Request, project: Project, ocr: OcrSessionRow) -> dict | None:
    """The Document's version, its document.md file, and how many questions are open."""
    if not ocr.document_version:
        return None
    return {
        "version": ocr.document_version,
        "artifact_id": ocr.document_artifact_id,
        "open_doubts": ocr_store.open_doubt_count(project.root, ocr),
    }


def _page(request: Request, project: Project, session_id: int, number: int) -> OcrPageRow:
    with store(request).db(project)() as session:
        page = session.scalars(
            select(OcrPageRow).where(
                OcrPageRow.session_id == session_id, OcrPageRow.number == number
            )
        ).first()
    if page is None:
        raise HTTPException(404, "No such page.")
    return page


def _ocr_row(request: Request, project: Project, session_id: int) -> OcrSessionRow:
    with store(request).db(project)() as session:
        return session.get(OcrSessionRow, session_id)


# --- starting and reading a session --------------------------------------------------------


def _needs_cleanups_model(request: Request) -> None:
    """On a Mac set up with MLX the usual chat model isn't on disk, and cleanup needs it. A
    chat's ocr_start downloads it (its card says so); here there's no card to ask on, so the
    session isn't started: it would read every page and then stop at the first cleanup."""
    entry = cleanup_model(request.app.state.runtime)
    if entry is not None and not request.app.state.downloads.is_downloaded(entry):
        raise HTTPException(
            409,
            f"Cleaning up scanned pages needs {entry.title} ({entry.size / 1e9:.1f} GB): the "
            "chat's MLX model can't do it. Download it in Models & memory, then start again.",
        )


@router.post("/projects/{project_id}/ocr", status_code=201)
async def new_ocr(project_id: int, body: NewOcr, request: Request, user: CurrentUser) -> dict:
    project = await run_in_threadpool(open_project, request, project_id, user)
    db = await run_in_threadpool(store(request).db, project)
    await run_in_threadpool(_needs_cleanups_model, request)
    try:
        row, _ = await run_in_threadpool(
            lambda: ocr_store.create_session(
                project.root, db, owner_id=user.id, artifact_id=body.artifact_id
            )
        )
    except ocr_store.CannotOpen as exc:
        raise HTTPException(400, str(exc)) from None
    await jobs(request).start(project, row.id)
    return await run_in_threadpool(_session_dict, request, project, row.id)


@router.get("/projects/{project_id}/ocr/models")
def ocr_models(project_id: int, request: Request, user: CurrentUser) -> list[dict]:
    """The OCR models "Re-run with…" offers, and whether each is downloaded."""
    open_project(request, project_id, user)
    downloads = request.app.state.downloads
    return [
        {
            "id": entry.id,
            "title": entry.title,
            "role": "ocr" if "ocr" in entry.roles else "ocr.hand",
            "downloaded": downloads.is_downloaded(entry),
        }
        for entry in catalog.load().models
        if catalog.OCR_ROLES & set(entry.roles)
    ]


@router.get("/projects/{project_id}/ocr/{session_id}")
def get_ocr(project_id: int, session_id: int, request: Request, user: CurrentUser) -> dict:
    project = _lookup(request, project_id, session_id, user)
    return _session_dict(request, project, session_id)


@router.get("/projects/{project_id}/ocr/{session_id}/pages/{number}/image")
def page_image(
    project_id: int, session_id: int, number: int, request: Request, user: CurrentUser
) -> FileResponse:
    project = _lookup(request, project_id, session_id, user)
    page = _page(request, project, session_id, number)
    path = ocr_store.page_image(project.root, _ocr_row(request, project, session_id), page)
    if not path.is_file():
        raise HTTPException(404, "The page image is no longer in the project folder.")
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "no-store"})


@router.get("/projects/{project_id}/ocr/{session_id}/pages/{number}/text")
def page_text(
    project_id: int, session_id: int, number: int, request: Request, user: CurrentUser
) -> dict:
    project = _lookup(request, project_id, session_id, user)
    page = _page(request, project, session_id, number)
    ocr = _ocr_row(request, project, session_id)
    text = ocr_store.page_text(project.root, ocr, page)
    document = ocr_store.load_document(project.root, ocr)
    cleaned = render_page(document, number) if document else None
    return {"number": number, "model_id": page.model_id, "text": text, "cleaned": cleaned}


@router.get("/projects/{project_id}/ocr/{session_id}/document")
def get_document(project_id: int, session_id: int, request: Request, user: CurrentUser) -> dict:
    """The session's Document (document.json), the source for M3b's review."""
    project = _lookup(request, project_id, session_id, user)
    document = ocr_store.load_document(project.root, _ocr_row(request, project, session_id))
    if document is None:
        raise HTTPException(404, "This session has no Document yet.")
    db = store(request).db(project)
    _, unplaced = match_decisions(document, ocr_store.decisions(db, session_id))
    # Decisions whose text is no longer on their page (it was read or cleaned again).
    return document.model_dump(by_alias=True) | {"unplaced": [asdict(d) for d in unplaced]}


# --- answering the Document's doubts (M3b) ---------------------------------------------------


@router.put("/projects/{project_id}/ocr/{session_id}/doubts/{doubt_id}")
async def answer_doubt(
    project_id: int,
    session_id: int,
    doubt_id: str,
    body: Answer,
    request: Request,
    user: CurrentUser,
) -> dict:
    """The person's reading for a doubt; the Document is published again as a new version.
    Whoever waits on the session hears of it: the last answer runs a held export."""
    project = await open_ocr(request, project_id, session_id, user)
    try:
        ocr, _ = await run_in_threadpool(
            lambda: ocr_store.decide(
                project.root, store(request).db(project), session_id, doubt_id,
                value=body.value, action=body.action, user_id=user.id,
            )
        )  # fmt: skip
    except ocr_store.NoSuchDoubt:
        raise HTTPException(404, "The Document has no such question.") from None
    published = await run_in_threadpool(_published, request, project, ocr)
    try:
        await jobs(request).tell(project, session_id)
    except Exception:  # noqa: BLE001 — the answer is saved; a waiting export hears it later
        log.exception("Telling about OCR session %s after an answer failed", session_id)
    return published


@router.delete("/projects/{project_id}/ocr/{session_id}/doubts/{doubt_id}")
def reopen_doubt(
    project_id: int, session_id: int, doubt_id: str, request: Request, user: CurrentUser
) -> dict:
    """Take back the answer to a doubt: it's an open question again, in a new version."""
    project = _lookup(request, project_id, session_id, user)
    try:
        ocr = ocr_store.reopen(project.root, store(request).db(project), session_id, doubt_id)
    except ocr_store.NoSuchDoubt:
        raise HTTPException(404, "No answered question has that id.") from None
    return _published(request, project, ocr)


def _published(request: Request, project: Project, ocr: OcrSessionRow) -> dict:
    summary = _document_summary(request, project, ocr)
    event = {"type": "document", "version": ocr.document_version,
             "artifact_id": ocr.document_artifact_id}  # fmt: skip
    jobs(request).channel((project.id, ocr.session_id)).publish(event)
    return summary


# --- changing pages, and the job ------------------------------------------------------------


@router.patch("/projects/{project_id}/ocr/{session_id}/pages/{number}")
def change_page(
    project_id: int,
    session_id: int,
    number: int,
    body: PageChange,
    request: Request,
    user: CurrentUser,
) -> dict:
    """The page's note (used by cleanup in M3), or marking it handwritten (its next reading
    goes to the ocr.hand model)."""
    project = _lookup(request, project_id, session_id, user)
    page = _page(request, project, session_id, number)
    with store(request).db(project).begin() as session:
        row = session.get(OcrPageRow, page.id)
        if body.note is not None:
            row.note = body.note
        if body.handwritten is not None:
            row.handwritten = body.handwritten
        row.updated_at = now()
        changed = page_dict(row)
    jobs(request).channel((project.id, session_id)).publish({"type": "page", **changed})
    return changed


@router.post("/projects/{project_id}/ocr/{session_id}/pages/{number}/rerun", status_code=202)
async def rerun_page(
    project_id: int,
    session_id: int,
    number: int,
    body: Rerun,
    request: Request,
    user: CurrentUser,
) -> dict:
    project = await open_ocr(request, project_id, session_id, user)
    await run_in_threadpool(_page, request, project, session_id, number)
    lease_key = None
    if body.model_id is not None:
        entry = catalog.load().get(body.model_id)
        if entry is None or not catalog.OCR_ROLES & set(entry.roles):
            raise HTTPException(400, f"{body.model_id!r} isn't an OCR model.")
        lease_key = f"{'ocr' if 'ocr' in entry.roles else 'ocr.hand'}@{entry.id}"
    page = await jobs(request).rerun(project, session_id, number, lease_key=lease_key)
    return page_dict(page)


@router.post("/projects/{project_id}/ocr/{session_id}/pages/{number}/clean", status_code=202)
async def clean_page_again(
    project_id: int, session_id: int, number: int, request: Request, user: CurrentUser
) -> dict:
    """Clean one page again, without reading it (its note changed), then assemble."""
    project = await open_ocr(request, project_id, session_id, user)
    page = await run_in_threadpool(_page, request, project, session_id, number)
    if page.status != "done":
        raise HTTPException(409, "This page hasn't been read yet.")
    page = await jobs(request).reclean(project, session_id, number)
    return page_dict(page)


@router.post("/projects/{project_id}/ocr/{session_id}/start-now")
async def start_now(project_id: int, session_id: int, request: Request, user: CurrentUser) -> dict:
    """Start now: the session's next page doesn't wait for the chat to be idle (it never
    takes a model in use)."""
    project = _lookup(request, project_id, session_id, user)
    await request.app.state.runtime.skip_hold()
    return await run_in_threadpool(_session_dict, request, project, session_id)


@router.post("/projects/{project_id}/ocr/{session_id}/pause")
async def pause(project_id: int, session_id: int, request: Request, user: CurrentUser) -> dict:
    project = await open_ocr(request, project_id, session_id, user)
    try:
        await jobs(request).pause(project, session_id)
    except NotNow as exc:
        raise HTTPException(409, str(exc)) from None
    return {"pausing": True}


@router.post("/projects/{project_id}/ocr/{session_id}/resume")
async def resume(project_id: int, session_id: int, request: Request, user: CurrentUser) -> dict:
    project = await open_ocr(request, project_id, session_id, user)
    try:
        await jobs(request).resume(project, session_id)
    except NotNow as exc:
        raise HTTPException(409, str(exc)) from None
    return {"resumed": True}


@router.get("/projects/{project_id}/ocr/{session_id}/events")
async def events(project_id: int, session_id: int, request: Request, user: CurrentUser):  # noqa: ANN201
    project = await open_ocr(request, project_id, session_id, user)
    channel = jobs(request).channel((project.id, session_id))

    async def stream() -> AsyncIterator[bytes]:
        async with channel.listen() as (so_far, queue):
            replayed = [*so_far, {"type": "replayed", "seq": 0}]
            async for chunk in pump(request, queue, replayed):
                yield chunk

    return event_response(stream())
