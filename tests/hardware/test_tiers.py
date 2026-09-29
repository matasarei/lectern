from __future__ import annotations

import pytest

from lectern.hardware.probe import GIB, Gpu, HardwareInfo, gpu_budget
from lectern.hardware.tiers import Tier, tier_for

MIB = 1024**2


def _pc(vram_bytes: int, ram_gib: float = 16 * 0.97) -> HardwareInfo:
    gpus = [Gpu("nvidia", "card", vram_bytes, vram_bytes, unified=False, cuda_driver="13.0")]
    return HardwareInfo(
        os="linux",
        arch="x86_64",
        cpu="cpu",
        cpu_cores=8,
        ram_total=int(ram_gib * GIB),
        ram_available=int(ram_gib * GIB) // 2,
        gpus=gpus,
        gpu_budget=gpu_budget(gpus),
    )


def _mac(ram_gib: int, budget_gib: float | None = None) -> HardwareInfo:
    ram = ram_gib * GIB
    budget = int(budget_gib * GIB) if budget_gib else int(ram * (2 / 3 if ram_gib <= 36 else 3 / 4))
    return HardwareInfo(
        os="macos",
        arch="arm64",
        cpu="Apple",
        cpu_cores=8,
        ram_total=ram,
        ram_available=ram // 2,
        gpus=[Gpu("apple", "Apple", ram, None, unified=True)],
        gpu_budget=budget,
    )


@pytest.mark.parametrize(
    ("vram", "expected"),
    [
        (2048 * MIB, Tier.BELOW_MINIMUM),
        (int(3.4 * GIB), Tier.BELOW_MINIMUM),
        (int(3.5 * GIB), Tier.MINIMUM),
        (4096 * MIB, Tier.MINIMUM),  # GTX 1650 / RTX 3050 4 GB
        (6144 * MIB, Tier.MINIMUM),
        (int(7.4 * GIB), Tier.MINIMUM),
        (8188 * MIB, Tier.RECOMMENDED),  # RTX 4060 reports a little under 8 GiB
        (12288 * MIB, Tier.RECOMMENDED),
        (int(14.9 * GIB), Tier.RECOMMENDED),
        (16376 * MIB, Tier.COMFORTABLE),
        (24576 * MIB, Tier.COMFORTABLE),
    ],
)
def test_discrete_gpu_tiers(vram, expected):
    assert tier_for(_pc(vram)) is expected


def test_low_ram_caps_a_pc_at_minimum():
    assert tier_for(_pc(8188 * MIB, ram_gib=8)) is Tier.MINIMUM


def test_low_ram_does_not_lift_below_minimum():
    assert tier_for(_pc(2048 * MIB, ram_gib=8)) is Tier.BELOW_MINIMUM


@pytest.mark.parametrize(
    ("ram_gib", "expected"),
    [
        (8, Tier.MINIMUM),
        (16, Tier.RECOMMENDED),
        (24, Tier.RECOMMENDED),
        (32, Tier.COMFORTABLE),
        (48, Tier.COMFORTABLE),
    ],
)
def test_apple_tiers_follow_the_gpu_budget(ram_gib, expected):
    assert tier_for(_mac(ram_gib)) is expected


def test_apple_raised_wired_limit_can_lift_the_tier():
    assert tier_for(_mac(16, budget_gib=12)) is Tier.RECOMMENDED
    assert tier_for(_mac(8, budget_gib=4)) is Tier.BELOW_MINIMUM


def test_cpu_only_is_below_minimum():
    hw = HardwareInfo(
        os="linux",
        arch="x86_64",
        cpu="cpu",
        cpu_cores=8,
        ram_total=32 * GIB,
        ram_available=16 * GIB,
    )
    assert tier_for(hw) is Tier.BELOW_MINIMUM


def test_lectern_tier_overrides_detection(monkeypatch):
    from lectern.hardware.tiers import effective_tier

    monkeypatch.setenv("LECTERN_TIER", "minimum")
    assert effective_tier(_mac(48)) is Tier.MINIMUM


def test_bad_lectern_tier_is_rejected(monkeypatch):
    from lectern.hardware.tiers import effective_tier

    monkeypatch.setenv("LECTERN_TIER", "huge")
    with pytest.raises(ValueError, match="LECTERN_TIER"):
        effective_tier(_mac(48))


def test_no_override_means_detection(monkeypatch):
    from lectern.hardware.tiers import effective_tier

    monkeypatch.delenv("LECTERN_TIER", raising=False)
    assert effective_tier(_mac(48)) is Tier.COMFORTABLE
