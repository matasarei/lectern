"""/api/projects: the projects list, creating a project, and renaming it.

Every route looks the project up for the current user; an unknown id and someone else's
project both answer 404, so ids can't be probed.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from lectern.core.auth import User, current_user
from lectern.projects.store import UNTITLED, Project, ProjectNotFound, ProjectStore
from lectern.projects.tables import SessionRow

router = APIRouter()

# The person asking (always the local user in the MVP; see lectern.core.auth).
CurrentUser = Annotated[User, Depends(current_user)]


def store(request: Request) -> ProjectStore:
    return request.app.state.projects


def open_project(request: Request, project_id: int, user: User) -> Project:
    try:
        return store(request).get(user, project_id)
    except ProjectNotFound:
        raise HTTPException(404, "No such project.") from None


def project_dict(project: Project) -> dict:
    created = project.created_at
    return {
        "id": project.id,
        "name": project.name,
        "folder": str(project.root),
        "created_at": created.isoformat() if isinstance(created, datetime) else None,
    }


class NewProject(BaseModel):
    name: str = Field(default=UNTITLED, max_length=400)


class Rename(BaseModel):
    name: str = Field(min_length=1, max_length=400)


@router.get("/projects")
def list_projects(request: Request, user: CurrentUser) -> list[dict]:
    return [project_dict(p) for p in store(request).list(user)]


@router.post("/projects", status_code=201)
def create_project(body: NewProject, request: Request, user: CurrentUser) -> dict:
    project = store(request).create(user, body.name)
    with store(request).db(project)() as session:
        chat = session.scalars(select(SessionRow).order_by(SessionRow.id)).first()
    return {**project_dict(project), "first_chat_id": chat.id if chat else None}


@router.get("/projects/{project_id}")
def get_project(project_id: int, request: Request, user: CurrentUser) -> dict:
    return project_dict(open_project(request, project_id, user))


@router.patch("/projects/{project_id}")
def rename_project(project_id: int, body: Rename, request: Request, user: CurrentUser) -> dict:
    try:
        return project_dict(store(request).rename(user, project_id, body.name))
    except ProjectNotFound:
        raise HTTPException(404, "No such project.") from None
