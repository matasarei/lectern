from __future__ import annotations

import pytest

from lectern.hardware.probe import GIB, Gpu, HardwareInfo
from lectern.runtime.wiring import CPU_RESERVE, budget_for

MIB = 1024**2


def _hw(gpu_budget=0):
    gpus = (
        [Gpu("nvidia", "RTX", 8 * GIB, 8 * GIB, unified=False, cuda_driver="13.0")]
        if gpu_budget
        else []
    )
    return HardwareInfo(
        os="linux",
        arch="x86_64",
        cpu="c",
        cpu_cores=8,
        ram_total=16 * GIB,
        ram_available=8 * GIB,
        gpus=gpus,
        gpu_budget=gpu_budget,
    )


def test_gpu_budget_by_default(monkeypatch):
    monkeypatch.delenv("LECTERN_BUDGET_MB", raising=False)
    assert budget_for(_hw(int(7.4 * GIB))) == (int(7.4 * GIB), "gpu")


def test_cpu_only_uses_ram_minus_the_reserve(monkeypatch):
    monkeypatch.delenv("LECTERN_BUDGET_MB", raising=False)
    assert budget_for(_hw()) == (16 * GIB - CPU_RESERVE, "ram")


def test_env_override(monkeypatch):
    monkeypatch.setenv("LECTERN_BUDGET_MB", "5000")
    assert budget_for(_hw(int(7.4 * GIB))) == (5000 * MIB, "LECTERN_BUDGET_MB")


@pytest.mark.parametrize("bad", ["abc", "-5", "0", "1.5", " "])
def test_bad_env_override_says_what_is_wrong(monkeypatch, bad):
    monkeypatch.setenv("LECTERN_BUDGET_MB", bad)
    with pytest.raises(ValueError, match="LECTERN_BUDGET_MB must be a whole number"):
        budget_for(_hw())
