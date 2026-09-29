"""Developer tools over HTTP. Only the swap test for now: it must share the server's runtime
so a chat can arrive in the middle of it."""

from __future__ import annotations

import asyncio
from dataclasses import asdict

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from lectern.dev.swap_test import SwapTestResult, run_swap_test

router = APIRouter(prefix="/dev")


class SwapTestRequest(BaseModel):
    pages: int = Field(default=12, ge=1, le=200)
    budget_mb: int | None = Field(default=None, ge=256)
    fake_model: bool = False


@router.post("/swap-test", status_code=202)
async def start_swap_test(body: SwapTestRequest, request: Request) -> dict:
    state = request.app.state
    running: SwapTestResult | None = getattr(state, "swap_test", None)
    if running is not None and not running.finished:
        raise HTTPException(409, "A swap test is already running.")
    runtime = state.runtime
    result = SwapTestResult(pages=body.pages)
    state.swap_test = result

    async def job() -> None:
        original_budget = runtime.budget
        if body.budget_mb:
            runtime.budget = body.budget_mb * 1024**2
        try:
            await runtime.unload_idle()  # start from nothing loaded
            await run_swap_test(runtime, result, fake_model=body.fake_model)
        finally:
            runtime.budget = original_budget

    state.swap_test_task = asyncio.get_running_loop().create_task(job())
    return asdict(result)


@router.get("/swap-test")
def get_swap_test(request: Request) -> dict:
    result: SwapTestResult | None = getattr(request.app.state, "swap_test", None)
    if result is None:
        raise HTTPException(404, "No swap test has run.")
    return asdict(result)
