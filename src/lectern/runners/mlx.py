"""MLX runners on Apple Silicon: `mlx_lm.server` for text, `mlx_vlm.server` for vision.

Off by default (setting `runtime.mlx_enabled`). `mlx_lm.server` has no API key, so any website
could call it through DNS rebinding; `mlx_vlm.server` accepts a key, but only on the command
line (visible in `ps`) and binds to 0.0.0.0 unless told otherwise. On a Mac, llama.cpp with
Metal is the default until those are solved.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import json
import platform
import secrets
import sys
from pathlib import Path

import httpx2

from lectern.runners.base import RunnerError, RunnerSpec, run_dir_name
from lectern.runners.llamacpp import READY_TIMEOUT, LlamaCppHandle, free_port
from lectern.runtime.planner import ModelShape

VISION_ROLES = frozenset({"ocr", "ocr.hand"})


def mlx_supported() -> bool:
    """MLX exists only for Apple Silicon Macs."""
    return sys.platform == "darwin" and platform.machine() == "arm64"


def mlx_available() -> bool:
    return mlx_supported() and importlib.util.find_spec("mlx_lm") is not None


def build_args(spec: RunnerSpec, *, vision: bool, port: int, key: str | None) -> list[str]:
    module = "mlx_vlm.server" if vision else "mlx_lm.server"
    args = [
        sys.executable, "-m", module,
        "--model", str(spec.model_file),
        "--host", "127.0.0.1",  # mlx_vlm.server would otherwise listen on 0.0.0.0
        "--port", str(port),
    ]  # fmt: skip
    if vision and key:
        args += ["--api-key", key]
    return args


def read_shape(model_dir: Path) -> ModelShape:
    """Layer and attention sizes from an MLX model's config.json."""
    config = json.loads((model_dir / "config.json").read_text())
    config = config.get("text_config", config)
    layers = int(config["num_hidden_layers"])
    heads = int(config["num_attention_heads"])
    kv_heads = int(config.get("num_key_value_heads", heads))
    head_dim = int(config.get("head_dim") or config["hidden_size"] // heads)
    interval = config.get("full_attention_interval")
    return ModelShape(
        layers=layers,
        kv_layers=layers // int(interval) if interval else layers,
        kv_heads=kv_heads,
        key_length=head_dim,
        value_length=head_dim,
    )


class MlxRunner:
    def __init__(self, work_dir: Path, *, vision: bool, ready_timeout: float = READY_TIMEOUT):
        self.work_dir = work_dir
        self.vision = vision
        self.ready_timeout = ready_timeout

    async def start(self, spec: RunnerSpec) -> LlamaCppHandle:
        run_dir = self.work_dir / run_dir_name(spec.model_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        log_file = run_dir / "server.log"
        port = free_port()
        key = secrets.token_urlsafe(32) if self.vision else None
        with log_file.open("wb") as log:
            process = await asyncio.create_subprocess_exec(
                *build_args(spec, vision=self.vision, port=port, key=key),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=log,
                stderr=asyncio.subprocess.STDOUT,
            )
        handle = LlamaCppHandle(
            model_id=spec.model_id, process=process, port=port, key=key or "", log_file=log_file
        )
        try:
            await self._wait_ready(handle)
        except BaseException:
            await handle.stop()
            raise
        return handle

    async def _wait_ready(self, handle: LlamaCppHandle) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.ready_timeout
        async with handle.client(timeout=2.0) as client:
            while loop.time() < deadline:
                if not handle.is_running():
                    raise RunnerError(
                        f"MLX server for {handle.model_id} exited.\n{handle.log_tail()}"
                    )
                with contextlib.suppress(httpx2.HTTPError):
                    if (await client.get("/v1/models")).status_code == 200:
                        return
                await asyncio.sleep(0.25)
        raise RunnerError(f"MLX server for {handle.model_id} wasn't ready.\n{handle.log_tail()}")
