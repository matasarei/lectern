"""GET /api/models: the catalog, with licence notices and the defaults for this machine's tier."""

from __future__ import annotations

from fastapi import APIRouter

from lectern.catalog import catalog
from lectern.hardware.probe import probe
from lectern.hardware.tiers import tier_for

router = APIRouter()


@router.get("/models")
def list_models() -> dict:
    cat = catalog.load()
    tier = tier_for(probe())
    defaults = {role: entry.id for role, entry in cat.defaults_for(tier).items()}
    return {
        "tier": tier.value,
        "defaults": defaults,
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
            }
            for m in cat.models
        ],
    }
