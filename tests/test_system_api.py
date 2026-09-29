from __future__ import annotations

from fastapi.testclient import TestClient

from lectern.api import system
from lectern.app import create_app
from lectern.hardware.probe import GIB, Gpu, HardwareInfo, gpu_budget
from lectern.hardware.sandbox import SandboxRuntime

MIB = 1024**2


def test_system_reports_hardware_tier_disk_and_sandbox(paths, monkeypatch):
    gpus = [Gpu("nvidia", "RTX 4060", 8188 * MIB, 7000 * MIB, unified=False, cuda_driver="13.0")]
    hw = HardwareInfo(
        os="linux",
        arch="x86_64",
        cpu="Ryzen",
        cpu_cores=8,
        ram_total=16 * GIB,
        ram_available=10 * GIB,
        gpus=gpus,
        gpu_budget=gpu_budget(gpus),
    )
    monkeypatch.setattr(system, "probe", lambda: hw)
    monkeypatch.setattr(
        system.sandbox,
        "detect",
        lambda: SandboxRuntime(runtime=None, version=None, engine_running=False, found=()),
    )
    app = create_app(paths)
    client = TestClient(app, base_url="http://127.0.0.1:8765")

    body = client.get("/api/system", headers={"X-Lectern-Token": app.state.token}).json()

    assert body["tier"] == "recommended"
    assert body["hardware"]["gpus"][0]["name"] == "RTX 4060"
    assert body["hardware"]["gpu_budget"] == gpu_budget(gpus)
    assert body["disk_free"] > 0
    assert body["sandbox"]["runtime"] is None
    assert body["tier_summary"]


def test_container_check_is_cached_between_requests(paths, monkeypatch):
    calls = []

    def fake_detect():
        calls.append(1)
        return SandboxRuntime(
            runtime="podman", version="5.4", engine_running=True, found=("podman",)
        )

    monkeypatch.setattr(system.sandbox, "detect", fake_detect)
    clock = [1000.0]
    monkeypatch.setattr(system.time, "monotonic", lambda: clock[0])
    app = create_app(paths)
    client = TestClient(app, base_url="http://127.0.0.1:8765")
    headers = {"X-Lectern-Token": app.state.token}

    client.get("/api/system", headers=headers)
    client.get("/api/system", headers=headers)
    assert len(calls) == 1

    clock[0] += system.SANDBOX_TTL_SECONDS + 1
    body = client.get("/api/system", headers=headers).json()
    assert len(calls) == 2
    assert body["sandbox"]["runtime"] == "podman"


def test_sandbox_check_skips_the_cache_and_refreshes_it(paths, monkeypatch):
    answers = iter(
        [
            SandboxRuntime(runtime="docker", version=None, engine_running=False, found=("docker",)),
            SandboxRuntime(
                runtime="docker", version="28.1", engine_running=True, found=("docker",)
            ),
        ]
    )
    monkeypatch.setattr(system.sandbox, "detect", lambda: next(answers))
    app = create_app(paths)
    client = TestClient(app, base_url="http://127.0.0.1:8765")
    headers = {"X-Lectern-Token": app.state.token}

    assert client.get("/api/system", headers=headers).json()["sandbox"]["engine_running"] is False
    # Docker started: the check sees it at once, although the cache is still fresh.
    checked = client.get("/api/system/sandbox", headers=headers).json()
    assert checked == {
        "runtime": "docker", "version": "28.1", "engine_running": True, "found": ["docker"]
    }  # fmt: skip
    # …and /system gives the new answer too, without another check.
    assert client.get("/api/system", headers=headers).json()["sandbox"]["engine_running"] is True


def test_sandbox_check_needs_the_token(paths):
    client = TestClient(create_app(paths), base_url="http://127.0.0.1:8765")
    assert client.get("/api/system/sandbox").status_code == 401
