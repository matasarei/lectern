"""MLX runners on Apple Silicon: `mlx_lm.server` for text, `mlx_vlm.server` for vision.

Off by default (setting `runtime.mlx_enabled`): on a Mac, llama.cpp with Metal is what a fresh
install runs, and MLX is a choice.

Both servers get a random key for each start, in their environment and never on the command
line, where `ps` shows it to every user. `mlx_vlm.server` reads its own
(MLX_VLM_SERVER_API_KEY). `mlx_lm.server` has no key at all and answers every website, so it is
started through `lectern.runners.mlx_text_server`, which puts the key in front of it. Both
listen on 127.0.0.1 because they are told to; mlx_vlm's default is 0.0.0.0.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import json
import os
import platform
import secrets
import sys
from pathlib import Path

import httpx2

from lectern.runners import mlx_text_server
from lectern.runners.base import RunnerError, RunnerSpec, run_dir_name
from lectern.runners.llamacpp import READY_TIMEOUT, LlamaCppHandle, free_port
from lectern.runtime.planner import ModelShape

VISION_ROLES = frozenset({"ocr", "ocr.hand"})
VISION_KEY_ENV = "MLX_VLM_SERVER_API_KEY"  # noqa: S105 (the variable's name, not a secret)
TEXT_KEY_ENV = mlx_text_server.KEY_ENV
NOT_INSTALLED = (
    "MLX isn't installed with this Lectern. The Mac installer adds it; from a checkout, run "
    "`uv sync --extra mlx` and start Lectern again."
)


def mlx_supported() -> bool:
    """MLX exists only for Apple Silicon Macs."""
    return sys.platform == "darwin" and platform.machine() == "arm64"


def mlx_available() -> bool:
    return mlx_supported() and importlib.util.find_spec("mlx_lm") is not None


def build_args(spec: RunnerSpec, *, vision: bool, port: int) -> list[str]:
    # Text goes through Lectern's own launcher: mlx_lm.server with a key in front of it.
    module = "mlx_vlm.server" if vision else mlx_text_server.__name__
    args = [
        sys.executable, "-m", module,
        "--model", str(spec.model_file),
        "--host", "127.0.0.1",  # mlx_vlm.server would otherwise listen on 0.0.0.0
        "--port", str(port),
    ]  # fmt: skip
    if not vision:
        # mlx_lm.server's default is every origin. Its own address is one no page is served
        # from, so no page is told it may read an answer.
        args += ["--allowed-origins", f"http://127.0.0.1:{port}"]
    return args


def build_env(key: str, *, vision: bool) -> dict[str, str]:
    """The server's environment: ours, the key under the name that server reads, and no
    reaching out to Hugging Face (the model is a folder on this disk)."""
    return {
        **os.environ,
        VISION_KEY_ENV if vision else TEXT_KEY_ENV: key,
        "HF_HUB_OFFLINE": "1",
    }


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
        key = secrets.token_urlsafe(32)
        with log_file.open("wb") as log:
            process = await asyncio.create_subprocess_exec(
                *build_args(spec, vision=self.vision, port=port),
                env=build_env(key, vision=self.vision),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=log,
                stderr=asyncio.subprocess.STDOUT,
            )
        handle = LlamaCppHandle(
            model_id=spec.model_id, process=process, port=port, key=key, log_file=log_file
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
                    # Text: /health, which answers at once (its /v1/models walks the Hugging
                    # Face cache). Vision is asked as before.
                    ready = "/v1/models" if self.vision else "/health"
                    status = (await client.get(ready)).status_code
                    if status == 200:
                        if not self.vision:
                            await self._load_model(client, handle, deadline - loop.time())
                        return
                    if status == 503 and not self.vision:
                        # mlx_lm.server's own word that the model couldn't be loaded: it
                        # answers 503 from then on, so there's nothing to wait for.
                        raise _not_loaded(handle)
                await asyncio.sleep(0.25)
        raise RunnerError(f"MLX server for {handle.model_id} wasn't ready.\n{handle.log_tail()}")

    async def _load_model(
        self, client: httpx2.AsyncClient, handle: LlamaCppHandle, timeout: float
    ) -> None:
        """mlx_lm.server answers /health while it's still loading the model, in a thread of
        its own, and a model it can't load leaves the server running. One token asked of the
        model returns when it's loaded, and fails when it can't be: only then is the server
        ready, and the failure is the start's, with the server's own words."""
        one_token = {"messages": [{"role": "user", "content": "Hi"}], "max_tokens": 1}
        try:
            response = await client.post("/v1/chat/completions", json=one_token, timeout=timeout)
            response.raise_for_status()
        except httpx2.HTTPError as exc:
            raise _not_loaded(handle) from exc


def _not_loaded(handle: LlamaCppHandle) -> RunnerError:
    return RunnerError(
        f"MLX server for {handle.model_id} didn't load the model.\n{handle.log_tail()}"
    )
