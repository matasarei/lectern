from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pytest
from gguf import GGUFWriter

from lectern.hardware.tiers import Tier
from lectern.runtime.planner import (
    CHAT_SPEED_FLOOR,
    DEFAULT_OVERHEAD,
    DISPLAY_MARGIN_MIB,
    MIN_CTX,
    DoesNotFit,
    ModelShape,
    kv_bytes,
    plan,
    predicted_tps,
    read_shape,
)

MIB = 1024**2
GIB = 1024**3

# Qwen3.5-4B, from its GGUF header: 32 layers, full attention every 4th, 4 KV heads of 256.
QWEN35_4B = ModelShape(layers=32, kv_layers=8, kv_heads=4, key_length=256, value_length=256)
QWEN35_4B_WEIGHTS = 2_740_937_888
# A dense 9B-class model: 36 layers, 8 KV heads of 128.
DENSE_9B = ModelShape(layers=36, kv_layers=36, kv_heads=8, key_length=128, value_length=128)
DENSE_9B_WEIGHTS = 5_680_000_000


def test_kv_matches_what_llama_cpp_allocated():
    # The real b11236 log for Qwen3.5-4B at -c 8192 -ctk/-ctv q8_0: "KV buffer size = 136.00 MiB".
    assert kv_bytes(QWEN35_4B, 8192, "q8_0") == 136 * MIB


def test_kv_f16_is_the_plain_formula():
    assert kv_bytes(DENSE_9B, 16384, "f16") == 36 * 8 * (128 + 128) * 16384 * 2


def _write_gguf(path, arch, **ints):
    writer = GGUFWriter(str(path), arch)
    for key, value in ints.items():
        writer.add_uint32(f"{arch}.{key}", value)
    writer.add_tensor("dummy", np.zeros(4, dtype=np.float32))
    writer.write_header_to_file()
    writer.write_kv_data_to_file()
    writer.write_tensors_to_file()
    writer.close()


def test_read_shape_of_a_hybrid_model(tmp_path):
    f = tmp_path / "hybrid.gguf"
    _write_gguf(
        f,
        "qwen35",
        **{
            "block_count": 32,
            "attention.head_count": 16,
            "attention.head_count_kv": 4,
            "attention.key_length": 256,
            "attention.value_length": 256,
            "embedding_length": 2560,
            "full_attention_interval": 4,
        },
    )
    assert read_shape(f) == QWEN35_4B


def test_read_shape_of_a_dense_model_without_key_length(tmp_path):
    f = tmp_path / "dense.gguf"
    _write_gguf(
        f,
        "llama",
        **{
            "block_count": 36,
            "attention.head_count": 32,
            "attention.head_count_kv": 8,
            "embedding_length": 4096,
        },
    )
    assert read_shape(f) == DENSE_9B


REAL_GGUF = os.environ.get("LECTERN_TEST_GGUF")


@pytest.mark.gpu
@pytest.mark.skipif(not REAL_GGUF, reason="needs a real GGUF")
def test_read_shape_of_the_real_file():
    shape = read_shape(Path(REAL_GGUF))
    assert shape.layers > 0 and shape.kv_layers > 0


def _plan(**overrides):
    args = dict(
        weights=DENSE_9B_WEIGHTS,
        shape=DENSE_9B,
        tier=Tier.RECOMMENDED,
        tier_ctx=16384,
        budget=int(7.4 * GIB),
        resident=0,
        unified=False,
    )
    return plan(**{**args, **overrides})


def test_fits_with_the_tiers_context_and_f16_when_there_is_room():
    p = _plan(budget=40 * GIB)
    assert (p.ctx, p.kv_type, p.on_gpu, p.coresident) == (16384, "f16", True, False)
    assert p.est_bytes == DENSE_9B_WEIGHTS + kv_bytes(DENSE_9B, 16384, "f16") + DEFAULT_OVERHEAD


def test_then_q8_kv_before_shrinking_the_context():
    f16 = DENSE_9B_WEIGHTS + kv_bytes(DENSE_9B, 16384, "f16") + DEFAULT_OVERHEAD
    p = _plan(budget=f16 - 1)
    assert (p.ctx, p.kv_type) == (16384, "q8_0")


def test_then_a_shorter_context_but_never_below_4096():
    at_4k = DENSE_9B_WEIGHTS + kv_bytes(DENSE_9B, MIN_CTX, "q8_0") + DEFAULT_OVERHEAD
    p = _plan(budget=at_4k)
    assert (p.ctx, p.kv_type, p.on_gpu) == (MIN_CTX, "q8_0", True)


def test_next_to_a_resident_model_when_both_fit():
    p = _plan(budget=40 * GIB, resident=1 * GIB)
    assert p.coresident and p.on_gpu


def test_needs_the_gpu_to_itself_when_both_dont_fit():
    alone = DENSE_9B_WEIGHTS + kv_bytes(DENSE_9B, 16384, "f16") + DEFAULT_OVERHEAD
    p = _plan(budget=alone, resident=2 * GIB)  # even q8_0 KV can't make room for 2 GiB
    assert not p.coresident and p.on_gpu and (p.ctx, p.kv_type) == (16384, "f16")


def test_spills_to_cpu_when_it_cannot_fit_alone_if_still_fast_enough():
    p = _plan(budget=int(5 * GIB), chat=True)
    assert not p.on_gpu and p.ctx == MIN_CTX
    assert p.predicted_tps is not None and p.predicted_tps >= CHAT_SPEED_FLOOR


def test_refuses_chat_below_the_speed_floor():
    with pytest.raises(DoesNotFit, match="tokens/s"):
        _plan(weights=20 * GIB, budget=2 * GIB, chat=True)


def test_background_jobs_may_run_slowly():
    p = _plan(weights=20 * GIB, budget=2 * GIB, chat=False)
    assert not p.on_gpu and p.predicted_tps < CHAT_SPEED_FLOOR


def test_fit_target_keeps_other_models_out_only_on_unified_memory():
    discrete = _plan(budget=40 * GIB, resident=2 * GIB, unified=False)
    unified = _plan(budget=40 * GIB, resident=2 * GIB, unified=True)
    assert discrete.fit_target_mib == DISPLAY_MARGIN_MIB
    assert unified.fit_target_mib == DISPLAY_MARGIN_MIB + 2048


def test_minimum_tier_chat_model_fits_a_4gb_card():
    # A 4 GB card leaves 3.4 GiB after the display margin.
    p = plan(
        weights=QWEN35_4B_WEIGHTS,
        shape=QWEN35_4B,
        tier=Tier.MINIMUM,
        tier_ctx=8192,
        budget=int(3.4 * GIB),
        resident=0,
        unified=False,
        chat=True,
    )
    assert p.on_gpu and p.ctx == 8192


def test_cpu_only_prediction_uses_cpu_bandwidth():
    assert predicted_tps(5 * 10**9, 0.0, Tier.BELOW_MINIMUM) == pytest.approx(5.0)


def test_sharing_may_cost_q8_kv_but_never_context():
    alone_f16 = DENSE_9B_WEIGHTS + kv_bytes(DENSE_9B, 16384, "f16") + DEFAULT_OVERHEAD
    alone_q8 = DENSE_9B_WEIGHTS + kv_bytes(DENSE_9B, 16384, "q8_0") + DEFAULT_OVERHEAD
    shared = _plan(budget=alone_q8 + GIB, resident=GIB)
    assert (shared.coresident, shared.ctx, shared.kv_type) == (True, 16384, "q8_0")
    assert alone_f16 > alone_q8
