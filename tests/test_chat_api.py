from __future__ import annotations

import json
from pathlib import Path

import httpx2
import pytest
from fastapi.testclient import TestClient

from lectern.app import create_app
from lectern.core import settings
from lectern.downloads.fetch import DownloadError
from lectern.hardware.tiers import Tier
from lectern.runners.base import MeasuredMemory, RunnerError
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


def test_runtime_settings_default_save_and_refuse_unimplemented(paths, monkeypatch):
    monkeypatch.setattr("lectern.runners.mlx.mlx_supported", lambda: True)
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


def test_mlx_switch_is_refused_off_apple_silicon(paths, monkeypatch):
    client, headers, _ = _app(paths)
    values = client.get("/api/runtime/settings", headers=headers).json()["values"]
    # Saved on before the switch was limited to Apple Silicon.
    settings.put(client.app.state.sessions, settings.RUNTIME_KEY, {**values, "mlx_enabled": True})
    monkeypatch.setattr("lectern.runners.mlx.mlx_supported", lambda: False)

    body = client.get("/api/runtime/settings", headers=headers).json()
    assert body["mlx_supported"] is False
    assert body["values"]["mlx_enabled"] is False  # reported off, so other saves still work

    refused = client.put(
        "/api/runtime/settings", json={**values, "mlx_enabled": True}, headers=headers
    )
    assert refused.status_code == 409 and "Apple Silicon" in refused.json()["detail"]
    saved = client.put("/api/runtime/settings", json=body["values"], headers=headers)
    assert saved.status_code == 200


def test_changing_the_mlx_switch_unloads_idle_models(paths, monkeypatch):
    monkeypatch.setattr("lectern.runners.mlx.mlx_supported", lambda: True)
    client, headers, _ = _app(paths)
    chat = {"messages": [{"role": "user", "content": "x"}]}

    def loaded() -> list[str]:
        residents = client.get("/api/runtime", headers=headers).json()["residents"]
        return [r["role"] for r in residents]

    values = client.get("/api/runtime/settings", headers=headers).json()["values"]
    client.post("/api/chat", json=chat, headers=headers)
    assert loaded() == ["main"]

    # Saved as it was: nothing a loaded model depends on has changed.
    assert client.put("/api/runtime/settings", json=values, headers=headers).status_code == 200
    assert loaded() == ["main"]

    # The switch changes which model a role runs on, and which copies are needed at all: what
    # is loaded and idle goes, and loads again by the new rule when it's next used.
    switched = {**values, "mlx_enabled": True}
    assert client.put("/api/runtime/settings", json=switched, headers=headers).status_code == 200
    assert loaded() == []

    client.post("/api/chat", json=chat, headers=headers)
    assert client.put("/api/runtime/settings", json=values, headers=headers).status_code == 200
    assert loaded() == []  # and the same when it's switched off again


def test_routes_reading_runtime_state_run_on_the_event_loop():
    import inspect

    from lectern.api import runtime as runtime_api

    # A sync route would run in a worker thread while the loop changes the runtime's dicts.
    assert inspect.iscoroutinefunction(runtime_api.get_runtime)
    assert inspect.iscoroutinefunction(runtime_api.runtime_events)


class _FailingRunner:
    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    async def start(self, spec):  # noqa: ANN001
        raise self.exc


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (
            RunnerError("llama-server for qwen3.5-4b exited with code 1.\nerror: bad magic"),
            "exited with code 1.",
        ),
        (
            DownloadError("llama.tar.gz: HTTP 503"),
            "HTTP 503",
        ),
    ],
)
def test_a_model_that_cannot_start_is_an_error_event(paths, exc, expected):
    def resolve(role):
        return ModelChoice(role, "qwen3.5-4b", Path("/m/q.gguf"), 2 * GIB, SHAPE, 8192)

    runtime = ModelRuntime(
        budget=8 * GIB,
        unified=False,
        tier=Tier.MINIMUM,
        resolve=resolve,
        runner_for=lambda c: _FailingRunner(exc),
    )
    app = create_app(paths, runtime=runtime)
    headers = {"X-Lectern-Token": app.state.token, "Origin": BASE}
    client = TestClient(app, base_url=BASE)

    events = _events(
        client.post(
            "/api/chat", json={"messages": [{"role": "user", "content": "x"}]}, headers=headers
        )
    )

    assert events[-1]["type"] == "error"
    assert events[-1]["detail"].endswith(expected)
    assert runtime.state()["waiting"] == 0


def test_a_malformed_chunk_ends_the_reply_with_an_error(paths):
    body = b'data: {"choices": [{"delta": {"content": "Hi"}}]}\n\ndata: {not json\n\n'
    client, headers, _ = _app(paths, FakeHandle(body=body))

    events = _events(
        client.post(
            "/api/chat", json={"messages": [{"role": "user", "content": "x"}]}, headers=headers
        )
    )

    assert [e["type"] for e in events] == ["model", "delta", "error"]
    assert events[-1]["detail"] == "The model server sent a malformed reply."
