"""NVIDIA GPUs through NVML (the library that ships with the driver)."""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from lectern.hardware.probe import Gpu


def _load_nvml() -> Any:
    import pynvml

    return pynvml


def cuda_version(driver_version: int) -> str:
    """NVML reports the CUDA driver version as e.g. 12040 for 12.4."""
    return f"{driver_version // 1000}.{(driver_version % 1000) // 10}"


def probe(nvml: Any | None = None) -> list[Gpu]:
    """All NVIDIA GPUs, or an empty list when there is no driver or no card."""
    from lectern.hardware.probe import Gpu

    nvml = nvml or _load_nvml()
    try:
        nvml.nvmlInit()
    except nvml.NVMLError:
        return []
    try:
        cuda = cuda_version(nvml.nvmlSystemGetCudaDriverVersion())
        gpus = []
        for index in range(nvml.nvmlDeviceGetCount()):
            handle = nvml.nvmlDeviceGetHandleByIndex(index)
            name = nvml.nvmlDeviceGetName(handle)
            memory = nvml.nvmlDeviceGetMemoryInfo(handle)
            capability = None
            # Only used to choose the llama.cpp build; if NVML can't say, the card still counts
            # and gets the build that covers the most GPUs.
            with contextlib.suppress(nvml.NVMLError):
                major, minor = nvml.nvmlDeviceGetCudaComputeCapability(handle)
                capability = f"{major}.{minor}"
            gpus.append(
                Gpu(
                    vendor="nvidia",
                    name=name.decode() if isinstance(name, bytes) else name,
                    memory_total=int(memory.total),
                    memory_free=int(memory.free),
                    unified=False,
                    cuda_driver=cuda,
                    compute_capability=capability,
                )
            )
        return gpus
    except nvml.NVMLError:
        return []
    finally:
        with contextlib.suppress(nvml.NVMLError):
            nvml.nvmlShutdown()
