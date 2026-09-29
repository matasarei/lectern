from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from lectern.core import settings
from lectern.core.db import make_engine, make_sessionmaker, migrate
from lectern.downloads.manager import DownloadManager
from lectern.hardware.probe import GIB, Gpu, HardwareInfo
from lectern.runners import mlx
from lectern.runners.base import RunnerSpec
from lectern.runners.llamacpp import InstallingLlamaCppRunner
from lectern.runtime.planner import ModelShape
from lectern.runtime.supervisor import ModelChoice, NotAvailable
from lectern.runtime.wiring import build_runtime

SPEC = RunnerSpec(
    model_id="m", model_file=Path("/models/m"), ctx=8192, kv_type="f16", fit_target_mib=512
)


def test_text_models_use_mlx_lm_on_localhost_without_a_key():
    args = mlx.build_args(SPEC, vision=False, port=4321, key=None)
    assert args[:3] == [sys.executable, "-m", "mlx_lm.server"]
    assert args[args.index("--host") + 1] == "127.0.0.1"
    assert "--api-key" not in args


def test_vision_models_use_mlx_vlm_on_localhost_with_a_key():
    args = mlx.build_args(SPEC, vision=True, port=4321, key="k3y")
    assert args[2] == "mlx_vlm.server"
    assert args[args.index("--host") + 1] == "127.0.0.1"  # its default would be 0.0.0.0
    assert args[args.index("--api-key") + 1] == "k3y"


def test_read_shape_from_config(tmp_path):
    (tmp_path / "config.json").write_text(
        json.dumps(
            {
                "text_config": {
                    "num_hidden_layers": 32,
                    "num_attention_heads": 16,
                    "num_key_value_heads": 4,
                    "head_dim": 256,
                    "hidden_size": 2560,
                    "full_attention_interval": 4,
                }
            }
        )
    )
    assert mlx.read_shape(tmp_path) == ModelShape(32, 8, 4, 256, 256)


def test_read_shape_without_head_dim(tmp_path):
    (tmp_path / "config.json").write_text(
        json.dumps(
            {
                "num_hidden_layers": 28,
                "num_attention_heads": 28,
                "num_key_value_heads": 4,
                "hidden_size": 3584,
            }
        )
    )
    assert mlx.read_shape(tmp_path) == ModelShape(28, 28, 4, 128, 128)


@pytest.fixture
def runtime(paths):
    engine = make_engine(paths.app_db)
    migrate(engine)
    sessions = make_sessionmaker(engine)
    hw = HardwareInfo(
        os="macos",
        arch="arm64",
        cpu="M4",
        cpu_cores=8,
        ram_total=48 * GIB,
        ram_available=24 * GIB,
        gpus=[Gpu("apple", "M4", 48 * GIB, None, unified=True)],
        gpu_budget=36 * GIB,
    )
    rt = build_runtime(paths, DownloadManager(paths.models, sessions), hw, sessions)
    return rt, sessions


def _choice(fmt, role="main"):
    return ModelChoice(role, "m", Path("/m"), GIB, ModelShape(8, 8, 1, 64, 64), 4096, format=fmt)


def test_mlx_is_never_chosen_while_the_setting_is_off(runtime, monkeypatch):
    rt, _ = runtime
    monkeypatch.setattr(mlx, "mlx_available", lambda: True)
    with pytest.raises(NotAvailable, match="MLX is off"):
        rt.runner_for(_choice("mlx"))


def test_mlx_needs_the_extra_installed(runtime, monkeypatch):
    rt, sessions = runtime
    settings.put(sessions, settings.RUNTIME_KEY, {"mlx_enabled": True})
    monkeypatch.setattr(mlx, "mlx_available", lambda: False)
    with pytest.raises(NotAvailable, match="lectern-ai\\[mlx\\]"):
        rt.runner_for(_choice("mlx"))


def test_mlx_is_used_when_enabled_available_and_the_model_is_mlx(runtime, monkeypatch):
    rt, sessions = runtime
    settings.put(sessions, settings.RUNTIME_KEY, {"mlx_enabled": True})
    monkeypatch.setattr(mlx, "mlx_available", lambda: True)

    runner = rt.runner_for(_choice("mlx", role="ocr"))

    assert isinstance(runner, mlx.MlxRunner) and runner.vision
    assert isinstance(rt.runner_for(_choice("gguf")), InstallingLlamaCppRunner)
