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
