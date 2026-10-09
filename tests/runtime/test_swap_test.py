from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import httpx2
import pytest
from fastapi.testclient import TestClient

from lectern.app import create_app
from lectern.dev.swap_test import SwapTestResult, run_swap_test, tiny_png
from lectern.hardware.tiers import Tier
from lectern.runners.base import MeasuredMemory
from lectern.runtime.planner import ModelShape
from lectern.runtime.supervisor import ModelChoice, ModelRuntime, Priority

GIB = 1024**3
SHAPE = ModelShape(layers=8, kv_layers=8, kv_heads=1, key_length=64, value_length=64)


class Handle:
    base_url = "http://127.0.0.1:1"
    pid = 1

    def __init__(self, model_id, bodies) -> None:  # noqa: ANN001
        self.model_id, self.bodies, self.alive = model_id, bodies, True

    def client(self) -> httpx2.AsyncClient:
        async def handler(request):
            await asyncio.sleep(0)  # a real model call waits on the network and yields
            self.bodies.append((self.model_id, json.loads(request.content)))
            return httpx2.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

        return httpx2.AsyncClient(base_url=self.base_url, transport=httpx2.MockTransport(handler))

    def is_running(self) -> bool:
        return self.alive

    def measured_memory(self) -> MeasuredMemory:
        return MeasuredMemory(device_bytes=None)

    async def stop(self) -> None:
        self.alive = False


class Runner:
    def __init__(self) -> None:
        self.started: list[str] = []
        self.bodies: list = []

    async def start(self, spec):  # noqa: ANN001
        await asyncio.sleep(0)
        self.started.append(spec.model_id)
        return Handle(spec.model_id, self.bodies)


def _runtime(budget_gib: float):
    runner = Runner()
    sizes = {"ocr": 1.5, "main": 5}

    def resolve(role):
        return ModelChoice(
            role, f"{role}-model", Path(f"/m/{role}.gguf"), int(sizes[role] * GIB), SHAPE, 4096
        )

    runtime = ModelRuntime(
        budget=int(budget_gib * GIB),
        unified=False,
        tier=Tier.RECOMMENDED,
        resolve=resolve,
        runner_for=lambda c: runner,
    )
    return runtime, runner


@pytest.mark.parametrize(("budget", "swaps"), [(6, 1), (40, 0)])
def test_twelve_pages_swap_once_when_both_models_dont_fit(budget, swaps):
    runtime, runner = _runtime(budget)

    result = asyncio.run(run_swap_test(runtime, SwapTestResult(pages=12), fake_model=False))

    assert (result.done, result.swaps, result.error, result.finished) == (24, swaps, None, True)
    assert runner.started == ["ocr-model", "main-model"]


def test_ocr_pages_carry_an_image_and_main_pages_dont():
    runtime, runner = _runtime(40)
    asyncio.run(run_swap_test(runtime, SwapTestResult(pages=1), fake_model=False))
    by_model = dict(runner.bodies)
    assert by_model["ocr-model"]["messages"][0]["content"][0]["type"] == "image_url"
    assert isinstance(by_model["main-model"]["messages"][0]["content"], str)


def test_fake_model_loads_the_main_file_as_a_second_process():
    runtime, runner = _runtime(6)

    result = asyncio.run(run_swap_test(runtime, SwapTestResult(pages=3), fake_model=True))

    assert result.swaps == 1
    assert runner.started == ["main-model (as ocr)", "main-model"]
    assert runtime.resolve("ocr").model_id == "ocr-model"  # the override is undone


def test_a_chat_during_the_job_is_served_between_pages():
    runtime, runner = _runtime(6)
    served_at: list[tuple[str, int]] = []

    async def go():
        result = SwapTestResult(pages=6)
        job = asyncio.create_task(run_swap_test(runtime, result, fake_model=False))
        while result.done < 2:
            await asyncio.sleep(0)
        async with runtime.lease("main", Priority.CHAT):
            served_at.append((result.phase, result.done))
        await job
        return result

    result = asyncio.run(go())
    assert result.done == 12 and result.error is None
    # Served during the OCR phase, one page after it arrived: not at the end of the phase,
    # and not after the whole job.
    assert served_at == [("ocr", 3)]
    assert result.swaps == 3  # ocr out for the chat, main out for the job, then the phase swap


def test_tiny_png_is_a_png():
    data = tiny_png()
    assert data.startswith(b"\x89PNG\r\n\x1a\n") and data.endswith(b"IEND\xaeB`\x82")


def test_swap_test_runs_through_the_api(paths):
    runtime, runner = _runtime(40)
    app = create_app(paths, runtime=runtime, dev=True)
    headers = {"X-Lectern-Token": app.state.token, "Origin": "http://127.0.0.1:8765"}
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        started = client.post(
            "/api/dev/swap-test", json={"pages": 3, "budget_mb": 6 * 1024}, headers=headers
        )
        assert started.status_code == 202
        for _ in range(200):
            result = client.get("/api/dev/swap-test", headers=headers).json()
            if result["finished"]:
                break
            time.sleep(0.01)
    assert (result["done"], result["swaps"], result["error"]) == (6, 1, None)
    assert runtime.budget == 40 * GIB  # the test budget is put back


def _dev_routes(app) -> tuple[int, dict]:  # noqa: ANN001
    """Starting a swap test and reading it back, with a valid token: (start status, read body)."""
    headers = {"X-Lectern-Token": app.state.token, "Origin": "http://127.0.0.1:8765"}
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        read = client.get("/api/dev/swap-test", headers=headers).json()
        started = client.post("/api/dev/swap-test", json={"pages": 1}, headers=headers)
    return started.status_code, read


@pytest.mark.parametrize("value", [None, "0", "", "true"])
def test_dev_routes_are_off_unless_lectern_dev_is_1(paths, monkeypatch, value):
    if value is None:
        monkeypatch.delenv("LECTERN_DEV", raising=False)
    else:
        monkeypatch.setenv("LECTERN_DEV", value)
    runtime, _ = _runtime(40)
    started, read = _dev_routes(create_app(paths, runtime=runtime))
    # Only the web catch-all is left at that path: it refuses POST and answers GET with 404.
    assert started == 405
    assert read == {"detail": "Not found"}
    assert runtime.budget == 40 * GIB  # nothing ran


def test_lectern_dev_1_turns_the_dev_routes_on(paths, monkeypatch):
    monkeypatch.setenv("LECTERN_DEV", "1")
    runtime, _ = _runtime(40)
    app = create_app(paths, runtime=runtime)
    headers = {"X-Lectern-Token": app.state.token, "Origin": "http://127.0.0.1:8765"}
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        read = client.get("/api/dev/swap-test", headers=headers)
    assert (read.status_code, read.json()) == (404, {"detail": "No swap test has run."})
