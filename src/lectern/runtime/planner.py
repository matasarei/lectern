"""The fit planner: how to load a model into the memory that's left.

Pure functions. The order of tries (docs/models.md, "Fitting a model"): the tier's context
with an f16 KV cache, then a q8_0 KV cache, then shorter contexts (never below 4096). If even
that doesn't fit on the GPU, llama.cpp's `--fit` moves layers to the CPU; the planner only
checks that the result would still be fast enough for chat.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from lectern.hardware.tiers import Tier

MIB = 1024**2
GIB = 1024**3
MIN_CTX = 4096
DEFAULT_OVERHEAD = 300 * MIB  # compute buffers etc.; replaced by the measured value later
DISPLAY_MARGIN_MIB = 512  # what llama.cpp's --fit leaves free for the desktop
KV_BYTES_PER_ELEMENT = {"f16": 2.0, "q8_0": 34 / 32}  # q8_0: 32 values + a 2-byte scale
CHAT_SPEED_FLOOR = 8.0  # tokens/s below which chat isn't usable

# Rough memory bandwidth, used only to predict speed when layers spill to the CPU.
GPU_BANDWIDTH = {
    Tier.BELOW_MINIMUM: 0.0,
    Tier.MINIMUM: 150e9,
    Tier.RECOMMENDED: 250e9,
    Tier.COMFORTABLE: 400e9,
}
CPU_BANDWIDTH = 25e9


@dataclass(frozen=True)
class ModelShape:
    layers: int
    kv_layers: int  # layers with a KV cache (fewer in hybrid models)
    kv_heads: int
    key_length: int
    value_length: int


@dataclass(frozen=True)
class FitPlan:
    ctx: int
    kv_type: str
    est_bytes: int
    fit_target_mib: int
    coresident: bool  # fits next to the models already loaded
    on_gpu: bool  # False: llama.cpp will spill layers to the CPU
    predicted_tps: float | None  # only when spilling


class DoesNotFit(Exception):
    pass


def read_shape(gguf_file: Path) -> ModelShape:
    """Layer and attention sizes from the GGUF header."""
    from gguf import GGUFReader

    fields = GGUFReader(gguf_file).fields

    def scalar(key: str) -> int | None:
        field = fields.get(key)
        if field is None or not field.data:
            return None
        return int(field.parts[field.data[0]][0])

    arch_field = fields["general.architecture"]
    arch = bytes(arch_field.parts[arch_field.data[0]]).decode()
    layers = scalar(f"{arch}.block_count") or 0
    heads = scalar(f"{arch}.attention.head_count") or 1
    kv_heads = scalar(f"{arch}.attention.head_count_kv") or heads
    embedding = scalar(f"{arch}.embedding_length") or 0
    head_dim = embedding // heads if heads else 0
    interval = scalar(f"{arch}.full_attention_interval")
    return ModelShape(
        layers=layers,
        kv_layers=layers // interval if interval else layers,
        kv_heads=kv_heads,
        key_length=scalar(f"{arch}.attention.key_length") or head_dim,
        value_length=scalar(f"{arch}.attention.value_length") or head_dim,
    )


def kv_bytes(shape: ModelShape, ctx: int, kv_type: str) -> int:
    per_token = shape.kv_layers * shape.kv_heads * (shape.key_length + shape.value_length)
    return int(per_token * ctx * KV_BYTES_PER_ELEMENT[kv_type])


def _candidates(tier_ctx: int) -> list[tuple[int, str]]:
    tries = [(tier_ctx, "f16"), (tier_ctx, "q8_0")]
    ctx = tier_ctx // 2
    while ctx >= MIN_CTX:
        tries.append((ctx, "q8_0"))
        ctx //= 2
    if tier_ctx > MIN_CTX and tries[-1][0] != MIN_CTX:
        tries.append((MIN_CTX, "q8_0"))
    return tries


def predicted_tps(weights: int, gpu_share: float, tier: Tier) -> float:
    """Tokens per second from memory bandwidth: every token reads all weights once."""
    gpu_bw = GPU_BANDWIDTH[tier]
    seconds = (1 - gpu_share) * weights / CPU_BANDWIDTH
    if gpu_share > 0 and gpu_bw > 0:
        seconds += gpu_share * weights / gpu_bw
    return 1 / seconds if seconds > 0 else float("inf")


def plan(
    *,
    weights: int,
    shape: ModelShape,
    tier: Tier,
    tier_ctx: int,
    budget: int,
    resident: int,
    unified: bool,
    overhead: int = DEFAULT_OVERHEAD,
    chat: bool = False,
) -> FitPlan:
    """Choose context and KV type. `budget` is what models may use in total, `resident` what the
    already-loaded models hold. Raises DoesNotFit when only a CPU spill is left and chat would
    fall below the speed floor."""

    def fit_target(res: int) -> int:
        # Discrete GPUs report free memory after other processes' allocations; Apple's unified
        # memory doesn't, so there the other models must be kept out explicitly.
        return DISPLAY_MARGIN_MIB + (res // MIB if unified else 0)

    tries = _candidates(tier_ctx)
    # Sharing the GPU may cost a q8_0 KV cache, never context: a shorter context is only for a
    # model that has the GPU to itself. Otherwise the supervisor swaps.
    passes = [(resident, tries[:2]), (0, tries)] if resident > 0 else [(0, tries)]
    for res, options in passes:
        free = budget - res
        for ctx, kv_type in options:
            est = weights + kv_bytes(shape, ctx, kv_type) + overhead
            if est <= free:
                return FitPlan(ctx, kv_type, est, fit_target(res), res > 0, True, None)

    # Not even alone on the GPU: llama.cpp will spill layers to the CPU at the smallest context.
    ctx, kv_type = tries[-1]
    est = weights + kv_bytes(shape, ctx, kv_type) + overhead
    gpu_share = max(0.0, min(1.0, budget / est)) if budget > 0 else 0.0
    tps = predicted_tps(weights, gpu_share, tier)
    if chat and tps < CHAT_SPEED_FLOOR:
        raise DoesNotFit(
            f"Only {gpu_share:.0%} of the model fits on the GPU; chat would run at about "
            f"{tps:.1f} tokens/s, below {CHAT_SPEED_FLOOR:.0f}. Choose a smaller model."
        )
    return FitPlan(ctx, kv_type, est, fit_target(0), False, False, tps)
