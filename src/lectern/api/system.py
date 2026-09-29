"""GET /api/system: the machine, its tier, free disk space and the sandbox runtime."""

from __future__ import annotations

import shutil
import time

from fastapi import APIRouter, Request

from lectern.config import Paths
from lectern.hardware import sandbox
from lectern.hardware.probe import probe
from lectern.hardware.tiers import TIER_SUMMARY, effective_tier

SANDBOX_TTL_SECONDS = 60.0  # asking podman/docker can take seconds; the answer rarely changes


def system_report(paths: Paths, sandbox_runtime: sandbox.SandboxRuntime | None = None) -> dict:
    hw = probe()
    tier = effective_tier(hw)
    paths.home.mkdir(parents=True, exist_ok=True)
    return {
        "hardware": hw.to_dict(),
        "tier": tier.value,
        "tier_summary": TIER_SUMMARY[tier],
        "disk_free": shutil.disk_usage(paths.home).free,
        "sandbox": (sandbox_runtime or sandbox.detect()).to_dict(),
    }


router = APIRouter()


@router.get("/system")
def get_system(request: Request) -> dict:
    # A plain `def`: FastAPI runs it in a worker thread, so the sysctl/NVML/podman calls
    # don't block the event loop.
    return system_report(request.app.state.paths, _cached_sandbox(request))


@router.get("/system/sandbox")
def check_sandbox(request: Request) -> dict:
    """A fresh container runtime check, for the first-run card after Docker or Podman is
    started. It also refreshes the cached answer /system gives."""
    return _cached_sandbox(request, fresh=True).to_dict()


def _cached_sandbox(request: Request, *, fresh: bool = False) -> sandbox.SandboxRuntime:
    """The container runtime check, cached per running app for SANDBOX_TTL_SECONDS."""
    now = time.monotonic()
    cached = getattr(request.app.state, "sandbox_cache", None)
    if not fresh and cached is not None and now - cached[0] < SANDBOX_TTL_SECONDS:
        return cached[1]
    result = sandbox.detect()
    request.app.state.sandbox_cache = (now, result)
    return result
