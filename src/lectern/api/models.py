"""The model catalog over HTTP: list models, start a download, follow progress."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from lectern.api.sse import hub_stream
from lectern.catalog import catalog
from lectern.downloads.manager import DownloadManager
from lectern.hardware.probe import probe
from lectern.hardware.tiers import effective_tier
from lectern.runners import mlx

router = APIRouter()


def _manager(request: Request) -> DownloadManager:
    return request.app.state.downloads


@router.get("/models")
def list_models(request: Request) -> dict:
    cat = catalog.load()
    tier = effective_tier(probe())
    downloads = _manager(request)
    return {
        "tier": tier.value,
        "defaults": {role: entry.id for role, entry in cat.defaults_for(tier).items()},
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
                "download": downloads.state(m).to_dict(),
            }
            for m in cat.models
        ],
    }


class DownloadRequest(BaseModel):
    accept_licence: bool = False


@router.post("/models/{model_id}/download", status_code=202)
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


@router.get("/models/events")
async def model_events(request: Request) -> StreamingResponse:
    """Server-sent events with download progress. Read with fetch(); EventSource can't send
    the token header."""
    return hub_stream(request, _manager(request).events)
