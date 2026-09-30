"""/api/projects/{id}/ocr: OCR sessions — start one on a PDF or image, follow its job, read
its pages, write page notes, read a page again, pause and resume.

Starting returns at once (201); the job runs on the server and the tab follows it on the
session's events stream, which starts with the events so far.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

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
from lectern.modules.ocr import store as ocr_store
from lectern.modules.ocr.jobs import NotNow, OcrJobs, page_dict
from lectern.projects.store import Project
from lectern.projects.tables import OcrPageRow, OcrSessionRow, SessionRow, iso, now

router = APIRouter()
NOTE_LIMIT = 5000


def jobs(request: Request) -> OcrJobs:
    return request.app.state.ocr_jobs


class NewOcr(BaseModel):
    artifact_id: int


class PageChange(BaseModel):
    note: str | None = Field(default=None, max_length=NOTE_LIMIT)
    handwritten: bool | None = None


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
        "updated_at": iso(ocr.updated_at),
        "pages": [page_dict(p) for p in ocr_store.pages(db, session_id)],
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


@router.post("/projects/{project_id}/ocr", status_code=201)
async def new_ocr(project_id: int, body: NewOcr, request: Request, user: CurrentUser) -> dict:
    project = await run_in_threadpool(open_project, request, project_id, user)
    db = await run_in_threadpool(store(request).db, project)
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
    text = ocr_store.page_text(project.root, _ocr_row(request, project, session_id), page)
    return {"number": number, "model_id": page.model_id, "text": text}


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
