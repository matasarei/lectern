from __future__ import annotations

import json
from pathlib import Path

import httpx2
import pytest
from fastapi.testclient import TestClient

from lectern.app import create_app
from lectern.hardware.tiers import Tier
from lectern.runners.base import MeasuredMemory
from lectern.runtime.planner import ModelShape
from lectern.runtime.supervisor import ModelChoice, ModelRuntime, NotAvailable

BASE = "http://127.0.0.1:8765"
GIB = 1024**3
SHAPE = ModelShape(layers=8, kv_layers=8, kv_heads=1, key_length=64, value_length=64)


def _sse(*chunks: dict) -> bytes:
    lines = [f"data: {json.dumps(c)}\n\n" for c in chunks] + ["data: [DONE]\n\n"]
    return "".join(lines).encode()


REPLY = _sse(
    {"choices": [{"delta": {"role": "assistant"}}]},
    {"choices": [{"delta": {"content": "Hel"}}]},
    {"choices": [{"delta": {"content": "lo!"}}]},
    {"choices": [], "usage": {"completion_tokens": 2}, "timings": {"predicted_per_second": 41.5}},
)


class FakeHandle:
    model_id = "qwen3.5-4b"
    base_url = "http://127.0.0.1:1"
    pid = 4242

    def __init__(self, status: int = 200, body: bytes = REPLY) -> None:
        self.status, self.body, self.requests = status, body, []

    def client(self) -> httpx2.AsyncClient:
        def handler(request):
            self.requests.append(json.loads(request.content))
            return httpx2.Response(self.status, content=self.body)

        return httpx2.AsyncClient(base_url=self.base_url, transport=httpx2.MockTransport(handler))

    def is_running(self) -> bool:
        return True

    def measured_memory(self) -> MeasuredMemory:
        return MeasuredMemory(device_bytes=3 * GIB)

    async def stop(self) -> None:
        pass


class FakeRunner:
    def __init__(self, handle: FakeHandle) -> None:
        self.handle = handle

    async def start(self, spec):  # noqa: ANN001
        return self.handle


def _app(paths, handle=None, *, available=True):
    handle = handle or FakeHandle()

    def resolve(role):
        if not available:
            raise NotAvailable("Qwen3.5 4B isn't downloaded yet. Download it first.")
        return ModelChoice(role, "qwen3.5-4b", Path("/m/q.gguf"), 2 * GIB, SHAPE, 8192)

    runtime = ModelRuntime(
        budget=8 * GIB,
        unified=False,
        tier=Tier.MINIMUM,
        resolve=resolve,
        runner_for=lambda c: FakeRunner(handle),
    )
    app = create_app(paths, runtime=runtime)
    headers = {"X-Lectern-Token": app.state.token, "Origin": BASE}
    return TestClient(app, base_url=BASE), headers, handle


def _events(response) -> list[dict]:
    return [
        json.loads(line.removeprefix("data: "))
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]


def test_chat_streams_the_reply_and_reports_timing(paths):
    client, headers, handle = _app(paths)

    response = client.post(
        "/api/chat", json={"messages": [{"role": "user", "content": "Hi"}]}, headers=headers
    )

    events = _events(response)
    assert events[0] == {"type": "model", "model_id": "qwen3.5-4b", "ctx": 8192}
    assert "".join(e["text"] for e in events if e["type"] == "delta") == "Hello!"
    done = events[-1]
    assert done["type"] == "done" and done["tps"] == 41.5 and done["tokens"] == 2
    assert done["ttft"] is not None and done["ttft"] >= 0
    sent = handle.requests[0]
    assert sent["stream"] is True
    assert sent["chat_template_kwargs"] == {"enable_thinking": False}


def test_model_not_available_is_an_error_event(paths):
    client, headers, _ = _app(paths, available=False)

    response = client.post(
        "/api/chat", json={"messages": [{"role": "user", "content": "Hi"}]}, headers=headers
    )

    assert _events(response) == [
        {"type": "error", "detail": "Qwen3.5 4B isn't downloaded yet. Download it first."}
    ]


def test_runner_error_is_passed_on(paths):
    client, headers, _ = _app(paths, FakeHandle(status=500, body=b'{"error":"out of memory"}'))

    events = _events(
        client.post(
            "/api/chat", json={"messages": [{"role": "user", "content": "x"}]}, headers=headers
        )
    )

    assert events[-1]["type"] == "error" and "out of memory" in events[-1]["detail"]


@pytest.mark.parametrize(
    "body",
    [
        {"messages": []},
        {"messages": [{"role": "tool", "content": "x"}]},
        {"messages": [{"role": "user", "content": "x"}], "max_tokens": 0},
    ],
)
def test_bad_chat_requests_are_rejected(paths, body):
    client, headers, _ = _app(paths)
    assert client.post("/api/chat", json=body, headers=headers).status_code == 422


def test_runtime_state_and_unload(paths):
    client, headers, _ = _app(paths)
    client.post("/api/chat", json={"messages": [{"role": "user", "content": "x"}]}, headers=headers)

    state = client.get("/api/runtime", headers=headers).json()
    assert state["tier"] == "minimum"
    assert [(r["role"], r["model_id"], r["bytes"]) for r in state["residents"]] == [
        ("main", "qwen3.5-4b", 3 * GIB)
    ]

    assert client.post("/api/runtime/unload", json={}, headers=headers).json() == {
        "unloaded": ["main"]
    }
    assert client.get("/api/runtime", headers=headers).json()["residents"] == []


def test_chat_needs_the_token(paths):
    client, _, _ = _app(paths)
    response = client.post("/api/chat", json={"messages": [{"role": "user", "content": "x"}]})
    assert response.status_code == 401


def test_runtime_settings_default_save_and_refuse_unimplemented(paths):
    client, headers, _ = _app(paths)

    body = client.get("/api/runtime/settings", headers=headers).json()
    assert body["values"] == {
        "mlx_enabled": False,
        "when_models_dont_fit": "swap_between_phases",
        "chat_during_job": "pause_job_after_current_item",
    }

    saved = client.put(
        "/api/runtime/settings", json={**body["values"], "mlx_enabled": True}, headers=headers
    ).json()
    assert saved["values"]["mlx_enabled"] is True
    assert client.get("/api/runtime/settings", headers=headers).json()["values"]["mlx_enabled"]

    later = {**body["values"], "chat_during_job": "chat_waits"}
    refused = client.put("/api/runtime/settings", json=later, headers=headers)
    assert refused.status_code == 409 and "isn't available yet" in refused.json()["detail"]


def test_routes_reading_runtime_state_run_on_the_event_loop():
    import inspect

    from lectern.api import runtime as runtime_api

    # A sync route would run in a worker thread while the loop changes the runtime's dicts.
    assert inspect.iscoroutinefunction(runtime_api.get_runtime)
    assert inspect.iscoroutinefunction(runtime_api.runtime_events)
