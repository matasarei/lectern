from __future__ import annotations

from types import SimpleNamespace

import pytest

from lectern.hardware import apple, nvidia, sandbox
from lectern.hardware.probe import DISPLAY_MARGIN_BYTES, GIB, Gpu, probe

MIB = 1024**2


class FakeNvml:
    """Just enough of pynvml for the probe."""

    class NVMLError(Exception):
        pass

    def __init__(self, cards=(), *, cuda=13000, init_fails=False, capability=(8, 9)):
        self.cards = list(cards)
        self.capability = capability
        self.cuda = cuda
        self.init_fails = init_fails
        self.shut_down = False

    def nvmlInit(self):
        if self.init_fails:
            raise self.NVMLError("no driver")

    def nvmlShutdown(self):
        self.shut_down = True

    def nvmlSystemGetCudaDriverVersion(self):
        return self.cuda

    def nvmlDeviceGetCount(self):
        return len(self.cards)

    def nvmlDeviceGetHandleByIndex(self, index):
        return index

    def nvmlDeviceGetName(self, handle):
        return self.cards[handle][0]

    def nvmlDeviceGetMemoryInfo(self, handle):
        name, total, free = self.cards[handle]
        return SimpleNamespace(total=total, free=free)

    def nvmlDeviceGetCudaComputeCapability(self, handle):
        return self.capability


def test_nvidia_card_is_reported():
    nvml = FakeNvml([(b"NVIDIA GeForce RTX 4060", 8188 * MIB, 7000 * MIB)], cuda=12040)

    [gpu] = nvidia.probe(nvml)

    assert gpu == Gpu(
        vendor="nvidia",
        name="NVIDIA GeForce RTX 4060",
        memory_total=8188 * MIB,
        memory_free=7000 * MIB,
        unified=False,
        cuda_driver="12.4",
        compute_capability="8.9",
    )
    assert nvml.shut_down


def test_pascal_card_reports_its_compute_capability():
    nvml = FakeNvml([(b"NVIDIA GeForce GTX 1050 Ti", 4096 * MIB, 4022 * MIB)], capability=(6, 1))
    [gpu] = nvidia.probe(nvml)
    assert (gpu.cuda_driver, gpu.compute_capability) == ("13.0", "6.1")


def test_no_nvidia_driver_means_no_gpus():
    assert nvidia.probe(FakeNvml(init_fails=True)) == []


@pytest.mark.parametrize(("raw", "expected"), [(12040, "12.4"), (13000, "13.0"), (12080, "12.8")])
def test_cuda_version_format(raw, expected):
    assert nvidia.cuda_version(raw) == expected


def _sysctl(values):
    return lambda name: values.get(name)


def test_apple_default_budget_small_mac():
    mac = apple.probe(
        _sysctl(
            {
                "hw.memsize": str(16 * GIB),
                "hw.optional.arm64": "1",
                "machdep.cpu.brand_string": "Apple M2",
                "iogpu.wired_limit_mb": "0",
            }
        )
    )
    assert mac.apple_silicon
    assert mac.gpu_budget == int(16 * GIB * 2 / 3)


def test_apple_default_budget_big_mac():
    mac = apple.probe(_sysctl({"hw.memsize": str(48 * GIB), "hw.optional.arm64": "1"}))
    assert mac.gpu_budget == int(48 * GIB * 3 / 4)


def test_apple_wired_limit_overrides_default():
    mac = apple.probe(
        _sysctl(
            {"hw.memsize": str(16 * GIB), "hw.optional.arm64": "1", "iogpu.wired_limit_mb": "12288"}
        )
    )
    assert mac.gpu_budget == 12288 * MIB


def test_apple_probe_without_sysctl_is_none():
    assert apple.probe(_sysctl({})) is None


def test_linux_with_nvidia_budget_keeps_display_margin():
    card = Gpu("nvidia", "RTX 3050", 4096 * MIB, 3900 * MIB, unified=False, cuda_driver="12.4")

    hw = probe(os_name="linux", nvidia_probe=lambda: [card])

    assert hw.gpus == [card]
    assert hw.gpu_budget == 4096 * MIB - DISPLAY_MARGIN_BYTES


def test_linux_without_gpu_is_cpu_only():
    hw = probe(os_name="linux", nvidia_probe=lambda: [])
    assert hw.gpus == []
    assert hw.gpu_budget == 0


def test_macos_apple_silicon_is_unified_gpu():
    mac = apple.MacInfo(
        chip="Apple M4 Max", apple_silicon=True, ram_total=48 * GIB, wired_limit_mb=0
    )

    hw = probe(os_name="macos", apple_probe=lambda: mac)

    assert hw.cpu == "Apple M4 Max"
    assert [g.unified for g in hw.gpus] == [True]
    assert hw.gpu_budget == mac.gpu_budget


def test_intel_mac_is_cpu_only():
    mac = apple.MacInfo(
        chip="Intel Core i7", apple_silicon=False, ram_total=16 * GIB, wired_limit_mb=0
    )
    hw = probe(os_name="macos", apple_probe=lambda: mac)
    assert hw.gpus == []
    assert hw.gpu_budget == 0


def test_sandbox_prefers_podman_when_both_run():
    result = sandbox.detect(which=lambda n: f"/usr/bin/{n}", engine_version=lambda b, a: "5.4")
    assert (result.runtime, result.engine_running, result.found) == (
        "podman",
        True,
        ("podman", "docker"),
    )


def test_sandbox_falls_back_to_docker_when_podman_is_stopped():
    versions = {"/usr/bin/podman": None, "/usr/bin/docker": "29.4.3"}
    result = sandbox.detect(
        which=lambda n: f"/usr/bin/{n}", engine_version=lambda b, a: versions[b]
    )
    assert (result.runtime, result.version) == ("docker", "29.4.3")


def test_sandbox_installed_but_not_running():
    result = sandbox.detect(
        which=lambda n: "/usr/bin/docker" if n == "docker" else None,
        engine_version=lambda b, a: None,
    )
    assert (result.runtime, result.engine_running) == ("docker", False)


def test_sandbox_none_installed():
    result = sandbox.detect(which=lambda n: None, engine_version=lambda b, a: None)
    assert (result.runtime, result.engine_running, result.found) == (None, False, ())
