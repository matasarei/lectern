"""The model catalog over HTTP: list models and what's installed, choose a role's model, start
a download, remove a model, follow progress."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool
from starlette.requests import HTTPConnection

from lectern.api.sse import hub_stream
from lectern.api.ws import hub_socket
from lectern.catalog import catalog
from lectern.core import settings
from lectern.core.auth import require_admin
from lectern.downloads.manager import (
    DownloadManager,
    NoSuchFolder,
    NothingToUpdate,
    NotRemovable,
)
from lectern.hardware.probe import probe
from lectern.hardware.tiers import effective_tier
from lectern.projects.tables import iso
from lectern.runners import mlx
from lectern.runtime.supervisor import ModelInUse
from lectern.runtime.wiring import choices, cleans_pages, pick

router = APIRouter()


def _manager(request: HTTPConnection) -> DownloadManager:
    return request.app.state.downloads


@router.get("/models")
def list_models(request: Request) -> dict:
    cat = catalog.load()
    tier = effective_tier(probe())
    downloads = _manager(request)
    recommended = cat.defaults_for(tier)
    runtime = request.app.state.runtime
    chosen = runtime.chosen_models()
    uses = {role: pick(role, tier, chosen)[1].id for role in recommended}
    defaults = {role: entry.id for role, entry in recommended.items()}
    # Cleanup of scanned pages isn't a catalog role, and its default isn't a tier's: the
    # runtime says which chat model cleans when none is chosen for it.
    if (cleans_by_default := runtime.cleanup_default()) is not None:
        defaults[settings.CLEANUP] = cleans_by_default
        uses[settings.CLEANUP] = cleans_pages(runtime).id
    runtime_settings = settings.get(
        request.app.state.sessions, settings.RUNTIME_KEY, settings.RUNTIME_DEFAULTS
    )
    mlx_on = bool(runtime_settings["mlx_enabled"]) and mlx.mlx_available()

    def used_by(m: catalog.CatalogEntry) -> list[str]:
        """The roles that use the model now, cleanup of scanned pages among them. An MLX
        build is used by the roles of the model it stands in for, while MLX is on; never by
        cleanup, which it can't do."""
        stands_for = m.replaces if mlx_on else None
        return [
            role
            for role, model_id in uses.items()
            if model_id == m.id or (model_id == stands_for and role != settings.CLEANUP)
        ]

    return {
        "tier": tier.value,
        # The model recommended for each role on this computer, and the one the role uses:
        # the same unless another was chosen (PUT /models/roles/{role}).
        "defaults": defaults,
        "chosen": uses,
        # What each can be given (PUT /models/roles/{role}), as catalog ids.
        "choices": {role: [m.id for m in choices(role)] for role in uses},
        "models": [
            {
                "id": m.id,
                "title": m.title,
                "quant": m.quant,
                "roles": m.roles,
                "size": m.size,
                "licence": m.licence,
                "licence_notice": m.licence_notice,
                "notes": m.notes,
                "repo": m.repo,
                "format": m.format,
                "replaces": m.replaces,  # an MLX build: the model it stands in for
                "suits_tier": tier in m.ctx,  # listed for this computer's tier
                "download": downloads.state(m).to_dict(),
                "installed": _installed(downloads, m),  # None: nothing of it on disk
                "used_by": used_by(m),
                # The catalog pins a newer revision than the one on disk (POST …/update), and
                # that update while it runs or after it failed.
                "update_available": downloads.update_available(m),
                "update": _update(downloads, m),
            }
            for m in cat.models
        ],
        # Folders in the models directory that no catalog model owns (a model a later
        # Lectern dropped): they take room, and can be removed like a model.
        "unlisted": [
            {"name": name, "bytes": size}
            for name, size in downloads.unlisted({m.id for m in cat.models})
        ],
    }


def _installed(downloads: DownloadManager, entry: catalog.CatalogEntry) -> dict | None:
    installed = downloads.installed(entry)
    if installed is None:
        return None
    return {
        "revision": installed.revision,  # the model's version on disk; None: not recorded
        "at": iso(installed.at) if installed.at else None,
        "bytes": installed.bytes,
    }


def _update(downloads: DownloadManager, entry: catalog.CatalogEntry) -> dict | None:
    state = downloads.update_state(entry.id)
    return state.to_dict() if state is not None else None


class RoleModel(BaseModel):
    model_id: str | None = None  # None: the one recommended for this computer


@router.put("/models/roles/{role}", dependencies=[Depends(require_admin)])
async def set_role_model(role: str, body: RoleModel, request: Request) -> dict:
    """Choose the model a role uses. Any model with the role can be chosen, one this
    computer isn't meant for included: the runtime refuses what doesn't fit when it loads."""
    if role not in settings.MODEL_DEFAULTS:
        raise HTTPException(404, f"There's no {role!r} role.")
    cat = catalog.load()
    model_id = body.model_id
    if model_id is not None:
        entry = cat.get(model_id)
        if entry is None:
            raise HTTPException(404, f"No model {model_id!r} in the catalog.")
        cleanup = role == settings.CLEANUP
        if entry.replaces is not None and cleanup:
            raise HTTPException(
                409,
                f"{entry.title} is an MLX build, which can't be held to the shape cleanup "
                f"needs. Choose {entry.replaces}.",
            )
        if entry.replaces is not None:
            raise HTTPException(
                409,
                f"{entry.title} is an MLX build. Choose {entry.replaces} and switch MLX on: "
                "the build is used in its place.",
            )
        if entry not in choices(role):
            raise HTTPException(
                409,
                f"{entry.title} can't clean up scanned pages: a chat model does that."
                if cleanup
                else f"{entry.title} can't be used for the {role!r} role.",
            )
        if model_id == await run_in_threadpool(_recommended, request, role):
            model_id = None  # the recommended one: the role follows the recommendation again
    before = await run_in_threadpool(
        settings.update,
        request.app.state.sessions,
        settings.MODELS_KEY,
        settings.MODEL_DEFAULTS,
        {role: model_id},
    )
    if before[role] != model_id:
        # The role's idle model goes now and the chosen one loads when the role is next used;
        # a model a reply is being written with changes over with the next turn. Cleanup's
        # copies are leased as the chat role's ("main", "main@<model>"): the chat's idle
        # model goes with them, and loads again with the next reply. A copy cleaning a page
        # goes as that page ends; the next page loads the one chosen.
        leased_as = "main" if role == settings.CLEANUP else role
        await request.app.state.runtime.unload_between_replies(leased_as)
    return await run_in_threadpool(list_models, request)


def _recommended(request: Request, role: str) -> str | None:
    """The catalog id of the model the role, or cleanup, uses when none is chosen for it."""
    if role == settings.CLEANUP:
        return request.app.state.runtime.cleanup_default()
    entry = catalog.load().defaults_for(effective_tier(probe())).get(role)
    return entry.id if entry is not None else None


class DownloadRequest(BaseModel):
    accept_licence: bool = False


@router.post("/models/{model_id}/download", status_code=202, dependencies=[Depends(require_admin)])
async def start_download(
    model_id: str, request: Request, body: DownloadRequest | None = None
) -> dict:
    entry = catalog.load().get(model_id)
    if entry is None:
        raise HTTPException(404, f"No model {model_id!r} in the catalog.")
    if entry.format == "mlx" and not mlx.mlx_supported():
        # The page doesn't offer it here; a request made by hand shouldn't fill the disk
        # with a model nothing on this machine can load.
        raise HTTPException(
            409, f"{entry.title} is an MLX model, and MLX only runs on Apple Silicon Macs."
        )
    if entry.licence_notice and not (body and body.accept_licence):
        raise HTTPException(
            409,
            f"{entry.title} is under {entry.licence}. Read the licence, then download again "
            "with accept_licence set to true.",
        )
    return _manager(request).start(entry).to_dict()


@router.post("/models/{model_id}/update", status_code=202, dependencies=[Depends(require_admin)])
async def update_model(model_id: str, request: Request) -> dict:
    """Bring an installed model to the revision the catalog pins now. It runs in the
    background: the new revision is built beside the model, which stays in use, and takes its
    place when the model is free. The list (`update`) says how far it is."""
    entry = catalog.load().get(model_id)
    if entry is None:
        raise HTTPException(404, f"No model {model_id!r} in the catalog.")
    try:
        return _manager(request).start_update(entry).to_dict()
    except NothingToUpdate as exc:
        raise HTTPException(
            409, f"{exc} It isn't on disk, or it is the catalog's version."
        ) from exc


@router.delete("/models/{name}", dependencies=[Depends(require_admin)])
async def remove_model(name: str, request: Request) -> dict:
    """Delete a model's folder from the models directory: a catalog model's, or one no
    catalog model owns. A role that used it keeps asking for it and shows Download."""
    downloads = _manager(request)
    entry = catalog.load().get(name)
    title = entry.title if entry is not None else name
    try:
        downloads.removable(name)  # before the runtime is asked to unload anything
        # No runner may have the files open, and none may load them half-deleted.
        await request.app.state.runtime.without_model(name, lambda: downloads.remove(name))
    except NoSuchFolder as exc:
        raise HTTPException(404, str(exc)) from exc
    except NotRemovable as exc:
        raise HTTPException(409, str(exc)) from exc
    except ModelInUse as exc:
        raise HTTPException(
            409, f"{title} is in use. Remove it when the reply or the job using it is done."
        ) from exc
    except OSError as exc:
        raise HTTPException(500, f"{title} couldn't be removed: {exc.strerror or exc}.") from exc
    return await run_in_threadpool(list_models, request)


@router.get("/models/events")
async def model_events(request: Request) -> StreamingResponse:
    """Server-sent events with download progress, for tools and tests. The page follows the
    socket below."""
    return hub_stream(request, _manager(request).events)


@router.websocket("/models/events")
async def model_events_socket(websocket: WebSocket) -> None:
    """Download progress for the page, which follows it on a socket (see `lectern.api.ws`)."""
    await hub_socket(websocket, _manager(websocket).events)
