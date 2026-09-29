"""GET /api/system: the machine, its tier, free disk space and the sandbox runtime."""

from __future__ import annotations

import shutil

from fastapi import APIRouter, Request

from lectern.config import Paths
from lectern.hardware import sandbox
from lectern.hardware.probe import probe
from lectern.hardware.tiers import TIER_SUMMARY, tier_for


def system_report(paths: Paths) -> dict:
    hw = probe()
    tier = tier_for(hw)
    paths.home.mkdir(parents=True, exist_ok=True)
    return {
        "hardware": hw.to_dict(),
        "tier": tier.value,
        "tier_summary": TIER_SUMMARY[tier],
        "disk_free": shutil.disk_usage(paths.home).free,
        "sandbox": sandbox.detect().to_dict(),
    }


router = APIRouter()


@router.get("/system")
def get_system(request: Request) -> dict:
    # A plain `def`: FastAPI runs it in a worker thread, so the sysctl/NVML/podman calls
    # don't block the event loop.
    return system_report(request.app.state.paths)
