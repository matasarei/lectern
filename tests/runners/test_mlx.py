from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import httpx2
import pytest

from lectern.core import settings
from lectern.core.db import make_engine, make_sessionmaker, migrate
from lectern.downloads.manager import DownloadManager
from lectern.hardware.probe import GIB, Gpu, HardwareInfo
from lectern.runners import mlx
from lectern.runners.base import RunnerError, RunnerSpec
from lectern.runners.llamacpp import InstallingLlamaCppRunner
from lectern.runtime.planner import ModelShape
from lectern.runtime.supervisor import ModelChoice, NotAvailable
from lectern.runtime.wiring import build_runtime

SPEC = RunnerSpec(
    model_id="m", model_file=Path("/models/m"), ctx=8192, kv_type="f16", fit_target_mib=512
)


def test_text_models_go_through_lecterns_launcher_on_localhost():
    args = mlx.build_args(SPEC, vision=False, port=4321)
    # Not mlx_lm.server itself: that one has no key and answers every website.
    assert args[:3] == [sys.executable, "-m", "lectern.runners.mlx_text_server"]
    assert args[args.index("--host") + 1] == "127.0.0.1"
    assert "--api-key" not in args
    # Its own address, which no page is served from, in place of the default "*".
    assert args[args.index("--allowed-origins") + 1] == "http://127.0.0.1:4321"


def test_vision_models_use_mlx_vlm_on_localhost():
    args = mlx.build_args(SPEC, vision=True, port=4321)
    assert args[2] == "mlx_vlm.server"
    assert args[args.index("--host") + 1] == "127.0.0.1"  # its default would be 0.0.0.0
    assert "--api-key" not in args  # on the command line, `ps` would show it to every user
    assert "--allowed-origins" not in args  # mlx_lm.server's flag, not this server's


@pytest.mark.parametrize(
    ("vision", "name", "other"),
    [(True, mlx.VISION_KEY_ENV, mlx.TEXT_KEY_ENV), (False, mlx.TEXT_KEY_ENV, mlx.VISION_KEY_ENV)],
)
def test_the_key_goes_in_the_servers_environment_under_the_name_it_reads(
    monkeypatch, vision, name, other
):
    monkeypatch.setenv("LECTERN_HOME", "/somewhere")
    monkeypatch.delenv(other, raising=False)
    env = mlx.build_env("k3y", vision=vision)
    assert env[name] == "k3y" and other not in env
    assert env["LECTERN_HOME"] == "/somewhere"  # the rest of ours is passed on
    assert env["HF_HUB_OFFLINE"] == "1"  # the model is a folder here: nothing to fetch


@pytest.mark.parametrize("vision", [True, False])
def test_a_key_already_in_our_environment_is_replaced(monkeypatch, vision):
    name = mlx.VISION_KEY_ENV if vision else mlx.TEXT_KEY_ENV
    monkeypatch.setenv(name, "someone-elses")
    assert mlx.build_env("k3y", vision=vision)[name] == "k3y"


class _Started(Exception):
    """Raised by the fake process start, once it has seen what the server would get."""


@pytest.mark.parametrize("vision", [True, False])
def test_start_gives_the_key_to_the_server_only_through_its_environment(
    tmp_path, monkeypatch, vision
):
    seen = {}

    async def fake_exec(*argv, **kwargs):  # noqa: ANN002, ANN003, ANN202
        seen.update(argv=list(argv), env=kwargs.get("env"))
        raise _Started

    monkeypatch.setattr(mlx.asyncio, "create_subprocess_exec", fake_exec)
    with pytest.raises(_Started):
        asyncio.run(mlx.MlxRunner(tmp_path, vision=vision).start(SPEC))
    assert "--api-key" not in seen["argv"]
    key = seen["env"][mlx.VISION_KEY_ENV if vision else mlx.TEXT_KEY_ENV]
    assert len(key) >= 32  # a fresh key, for either server
    assert key not in " ".join(seen["argv"])  # in the environment only


class _Handle:
    """A started server, answered by `answer`."""

    model_id = "m"

    def __init__(self, answer) -> None:  # noqa: ANN001
        self.answer = answer

    def client(self, timeout: float = 600.0) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(
            base_url="http://127.0.0.1:1",
            transport=httpx2.MockTransport(self.answer),
            headers={"Authorization": "Bearer k3y"},
        )

    def is_running(self) -> bool:
        return True

    def log_tail(self, lines: int = 20) -> str:
        return "ValueError: Model type qwen9 not supported."


@pytest.mark.parametrize(
    ("vision", "paths"),
    [(False, ["/health", "/v1/chat/completions"]), (True, ["/v1/models"])],
)
def test_each_server_is_asked_whether_its_ready_where_it_answers(tmp_path, vision, paths):
    asked = []

    async def answer(request: httpx2.Request) -> httpx2.Response:
        await asyncio.sleep(0)
        asked.append((request.url.path, request.headers.get("Authorization")))
        return httpx2.Response(200)

    asyncio.run(mlx.MlxRunner(tmp_path, vision=vision)._wait_ready(_Handle(answer)))
    # With the key: the launcher refuses /health too without it.
    assert asked == [(path, "Bearer k3y") for path in paths]


def test_a_text_server_is_ready_when_its_model_has_answered(tmp_path):
    """mlx_lm.server answers /health while the model is still loading: one token is asked of
    the model, and that request returns when it's loaded."""
    bodies = []

    async def answer(request: httpx2.Request) -> httpx2.Response:
        await asyncio.sleep(0)
        if request.method == "POST":
            bodies.append(json.loads(request.content))
        return httpx2.Response(200)

    asyncio.run(mlx.MlxRunner(tmp_path, vision=False)._wait_ready(_Handle(answer)))
    assert [body["max_tokens"] for body in bodies] == [1]
    assert bodies[0]["messages"][0]["role"] == "user"


@pytest.mark.parametrize("failure", [httpx2.Response(500), httpx2.ReadError("closed")])
def test_a_text_model_that_does_not_load_is_a_failed_start(tmp_path, failure):
    async def answer(request: httpx2.Request) -> httpx2.Response:
        await asyncio.sleep(0)
        if request.method == "GET":
            return httpx2.Response(200)  # the server itself is up
        if isinstance(failure, Exception):
            raise failure
        return failure

    with pytest.raises(RunnerError) as raised:
        asyncio.run(mlx.MlxRunner(tmp_path, vision=False)._wait_ready(_Handle(answer)))
    assert "didn't load" in str(raised.value)
    assert "Model type qwen9 not supported" in str(raised.value)  # the server's own words


def test_a_text_server_that_says_its_model_failed_is_not_waited_for(tmp_path):
    """Seen with the real server: when the model can't be loaded, /health answers 503 from
    then on. Waiting out the timeout would also push the reason out of the log's last lines."""
    asked = []

    async def answer(request: httpx2.Request) -> httpx2.Response:
        await asyncio.sleep(0)
        asked.append(request.url.path)
        return httpx2.Response(503, json={"status": "unavailable"})

    with pytest.raises(RunnerError) as raised:
        asyncio.run(mlx.MlxRunner(tmp_path, vision=False)._wait_ready(_Handle(answer)))
    assert asked == ["/health"]
    assert "didn't load" in str(raised.value)
    assert "Model type qwen9 not supported" in str(raised.value)


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
def runtime(paths, monkeypatch):
    monkeypatch.setattr(mlx, "mlx_supported", lambda: True)  # this fixture is an Apple Silicon Mac
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


def test_mlx_models_are_refused_off_apple_silicon(runtime, monkeypatch):
    rt, sessions = runtime
    settings.put(sessions, settings.RUNTIME_KEY, {"mlx_enabled": True})
    monkeypatch.setattr(mlx, "mlx_supported", lambda: False)
    # Not "turn it on in Models & memory": the switch isn't shown there.
    with pytest.raises(NotAvailable, match="only runs on Apple Silicon"):
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
