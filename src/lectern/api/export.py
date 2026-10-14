"""Export without the chat: the Export buttons on a finished OCR session, the Document tab
and the chat's "finished" message. The same writer as export.run; where the chat asks before
a format loses something, this answers 409 with what it loses, and the page asks.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from lectern.agent.export_tools import write_export
from lectern.api.projects import CurrentUser, artifact_dict, open_project, store
from lectern.documents.export import (
    CAPABILITIES,
    ExportOptions,
    load_for_export,
    lost,
    resolved_options,
)
from lectern.projects import files
from lectern.projects.tables import ArtifactRow

router = APIRouter()


class ExportRequest(BaseModel):
    artifact_id: int  # the document.md of an OCR session, a Markdown or text file, a PDF…
    format: str
    open_doubts: Literal["comment", "inline"] | None = None
    notes: Literal["omit", "comment"] = "omit"
    accept_loss: bool = False  # the person saw what the format loses and said go on


def _exporters(request: Request) -> dict:
    return {e.format: e for e in request.app.state.exporters}


@router.get("/exporters")
def exporters(request: Request) -> list[dict]:
    """The installed formats, what each keeps, in name order."""
    return [
        {"format": e.format, "extension": e.extension,
         "capabilities": [c for c in CAPABILITIES if c in e.capabilities]}
        for _, e in sorted(_exporters(request).items())
    ]  # fmt: skip


@router.post("/projects/{project_id}/export", status_code=201, response_model=None)
async def export(
    project_id: int, body: ExportRequest, request: Request, user: CurrentUser
) -> dict | JSONResponse:
    project = await run_in_threadpool(open_project, request, project_id, user)
    sessions = await run_in_threadpool(store(request).db, project)
    exporter = _exporters(request).get(body.format.lower().lstrip("."))
    if exporter is None:
        raise HTTPException(400, f"No {body.format} exporter is installed.")

    def source() -> ArtifactRow | None:
        with sessions() as session:
            row = session.get(ArtifactRow, body.artifact_id)
            if row is not None:
                session.expunge(row)
            return row

    row = await run_in_threadpool(source)
    if row is None or row.trashed_at is not None or row.owner_id != user.id:
        raise HTTPException(404, "No such file.")
    try:
        doc = await run_in_threadpool(load_for_export, project.root, row.path)
    except (ValueError, files.BadName) as exc:
        raise HTTPException(400, str(exc)) from None
    opts = resolved_options(ExportOptions(open_doubts=body.open_doubts, notes=body.notes), exporter)
    missing = lost(doc, opts, exporter)
    if missing and not body.accept_loss:
        return JSONResponse(
            status_code=409,
            content={"detail": f"{exporter.format.upper()} has no {'; '.join(missing)}.",
                     "lost": missing},
        )  # fmt: skip
    written = await run_in_threadpool(
        write_export, project.root, sessions, user.id, exporter, doc, opts, row
    )
    return artifact_dict(written, row.path)
