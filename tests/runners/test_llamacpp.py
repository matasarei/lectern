from __future__ import annotations

import asyncio
import os
import stat
import sys
from pathlib import Path

import httpx2
import pytest

from lectern.runners.base import RunnerError, RunnerSpec
from lectern.runners.llamacpp import LlamaCppRunner, _write_key, build_args, parse_memory

FIXTURES = Path(__file__).parent.parent / "fixtures"
MIB = 1024**2


def _spec(**overrides):
    base = dict(
        model_id="qwen3.5-4b",
        model_file=Path("/models/qwen3.5-4b/Qwen3.5-4B-Q4_K_M.gguf"),
        ctx=8192,
        kv_type="q8_0",
        fit_target_mib=1024,
    )
    return RunnerSpec(**{**base, **overrides})


def test_args_bind_localhost_use_a_key_file_and_fit():
    args = build_args(_spec(), port=41234, key_file=Path("/run/key"), slot_dir=Path("/run/slots"))

    def value(flag):
        return args[args.index(flag) + 1]

    assert value("--host") == "127.0.0.1"
    assert value("--port") == "41234"
    assert value("--api-key-file") == "/run/key"
    assert "--api-key" not in args  # never the key itself on the command line
    assert (value("-c"), value("-ctk"), value("-ctv")) == ("8192", "q8_0", "q8_0")
    assert (value("--fit"), value("--fit-target")) == ("on", "1024")
    assert "--jinja" in args and value("--slot-save-path") == "/run/slots"
    assert value("-lv") == "4"  # needed for the memory lines
    assert "--mmproj" not in args


def test_vision_models_get_their_projector():
    args = build_args(
        _spec(mmproj=Path("/m/mmproj.gguf")), port=1, key_file=Path("k"), slot_dir=Path("s")
    )
    assert args[args.index("--mmproj") + 1] == "/m/mmproj.gguf"


def test_memory_from_the_real_b11236_log():
    memory = parse_memory((FIXTURES / "llama_server_startup_b11236.log").read_text())

    # MTL0: model + KV + recurrent state + compute; the CPU buffers are excluded
    assert memory.device_bytes == int((2603.50 + 136.00 + 201.00 + 110.73) * MIB)
    assert memory.projected_bytes == 3051 * MIB


def test_cuda_buffer_names_count_as_device_memory():
    log = (
        "load_tensors:        CUDA0 model buffer size =  5120.00 MiB\n"
        "load_tensors:   CPU_Mapped model buffer size =   300.00 MiB\n"
        "llama_kv_cache:      CUDA0 KV buffer size =   512.00 MiB\n"
    )
    assert parse_memory(log).device_bytes == int(5632 * MIB)


def test_a_log_without_memory_lines_reports_unknown():
    memory = parse_memory("llama_server: model loaded\n")
    assert (memory.device_bytes, memory.projected_bytes) == (None, None)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_key_file_is_owner_only(tmp_path):
    key = _write_key(tmp_path / "run" / "key")
    assert len(key) >= 40
    assert stat.S_IMODE((tmp_path / "run" / "key").stat().st_mode) == 0o600


def test_a_server_that_exits_is_reported_with_its_log(tmp_path):
    fake = tmp_path / "llama-server"
    fake.write_text("#!/bin/sh\necho 'error: model file not found'\nexit 3\n")
    fake.chmod(0o755)
    runner = LlamaCppRunner(fake, tmp_path / "work", ready_timeout=10)

    with pytest.raises(RunnerError, match="exited with code 3") as info:
        asyncio.run(runner.start(_spec()))
    assert "model file not found" in str(info.value)


def test_a_server_that_never_answers_times_out_and_is_stopped(tmp_path):
    fake = tmp_path / "llama-server"
    fake.write_text("#!/bin/sh\nsleep 30\n")
    fake.chmod(0o755)
    runner = LlamaCppRunner(fake, tmp_path / "work", ready_timeout=1)

    async def go():
        with pytest.raises(RunnerError, match="wasn't ready"):
            await runner.start(_spec())

    asyncio.run(go())


# --- a real llama-server -------------------------------------------------------------------

REAL_SERVER = os.environ.get("LECTERN_TEST_LLAMA_SERVER")
REAL_GGUF = os.environ.get("LECTERN_TEST_GGUF")


@pytest.mark.gpu
@pytest.mark.skipif(not (REAL_SERVER and REAL_GGUF), reason="needs a real llama-server and GGUF")
def test_real_server_starts_needs_its_key_reports_memory_and_stops(tmp_path):
    runner = LlamaCppRunner(Path(REAL_SERVER), tmp_path / "work")

    async def go():
        handle = await runner.start(_spec(model_file=Path(REAL_GGUF), ctx=4096, fit_target_mib=512))
        try:
            async with httpx2.AsyncClient(base_url=handle.base_url) as anonymous:
                assert (await anonymous.get("/health")).status_code == 200
                assert (await anonymous.get("/v1/models")).status_code == 401
            async with handle.client() as client:
                assert (await client.get("/v1/models")).status_code == 200
            memory = handle.measured_memory()
            assert memory.projected_bytes and memory.projected_bytes > 0
            return handle
        finally:
            await handle.stop()

    handle = asyncio.run(go())
    assert not handle.is_running()
