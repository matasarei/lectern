"""Hardware tiers: which model sizes a machine gets by default.

The thresholds sit a little below the marketed sizes, because cards report less than
they're sold as (an "8 GB" card typically shows about 8188 MiB) and a 16 GB Mac gives
the GPU about 10.7 GB.
"""

from __future__ import annotations

import os
from enum import StrEnum

from lectern.hardware.probe import GIB, HardwareInfo


class Tier(StrEnum):
    BELOW_MINIMUM = "below_minimum"
    MINIMUM = "minimum"
    RECOMMENDED = "recommended"
    COMFORTABLE = "comfortable"


# (tier, smallest discrete VRAM in GiB, smallest Apple GPU budget in GiB), best first
THRESHOLDS: tuple[tuple[Tier, float, float], ...] = (
    (Tier.COMFORTABLE, 15.0, 20.0),  # 16 GB cards; Macs with 32 GB and more
    (Tier.RECOMMENDED, 7.5, 10.0),  # 8 GB cards; 16 GB Macs
    (Tier.MINIMUM, 3.5, 5.0),  # 4 GB cards; 8 GB Macs
)

# A PC tier assumes 16 GB of system RAM (16 GB machines report a little under 16 GiB).
MIN_RAM_FOR_TIER_GIB = 14.0

TIER_SUMMARY = {
    Tier.BELOW_MINIMUM: "No usable GPU. Models run on the CPU: it works, but slowly.",
    Tier.MINIMUM: "4B chat model, one model on the GPU at a time.",
    Tier.RECOMMENDED: "9B chat model; OCR swaps with the chat model.",
    Tier.COMFORTABLE: "Larger models; chat and OCR stay loaded together.",
}


def tier_for(hw: HardwareInfo) -> Tier:
    if not hw.gpus or hw.gpu_budget <= 0:
        return Tier.BELOW_MINIMUM
    best = max(hw.gpus, key=lambda g: g.memory_total)
    tier = Tier.BELOW_MINIMUM
    for candidate, min_vram_gib, min_apple_gib in THRESHOLDS:
        if best.unified:
            fits = hw.gpu_budget >= min_apple_gib * GIB
        else:
            fits = best.memory_total >= min_vram_gib * GIB
        if fits:
            tier = candidate
            break
    low_ram = hw.ram_total < MIN_RAM_FOR_TIER_GIB * GIB
    if not best.unified and tier is not Tier.BELOW_MINIMUM and low_ram:
        return Tier.MINIMUM
    return tier


def effective_tier(hw: HardwareInfo) -> Tier:
    """The tier the app uses: the detected one, unless LECTERN_TIER overrides it (for
    development and for testing a smaller tier on a bigger machine)."""
    override = os.environ.get("LECTERN_TIER")
    if override:
        try:
            return Tier(override)
        except ValueError:
            valid = ", ".join(t.value for t in Tier)
            raise ValueError(f"LECTERN_TIER={override!r} is not one of: {valid}") from None
    return tier_for(hw)
