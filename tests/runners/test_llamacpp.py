from __future__ import annotations

import asyncio
import os
import stat
import sys
from pathlib import Path

import httpx2
import pytest

from lectern.runners.base import RunnerError, RunnerSpec, run_dir_name
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


@pytest.mark.parametrize(
    ("model_id", "expected"),
    [
        ("qwen3.5-9b", "qwen3.5-9b"),  # catalog ids stay as they are
        ("Qwen3.5-9B_Q4", "Qwen3.5-9B_Q4"),
    ],
)
def test_run_dir_keeps_plain_ids(model_id, expected):
    assert run_dir_name(model_id) == expected


def test_run_dir_has_no_spaces_or_parentheses():
    name = run_dir_name("qwen3.5-4b (as ocr)")
    assert name.startswith("qwen3.5-4b-as-ocr-")
    assert all(c.isalnum() or c in "._-" for c in name)
    # Ids that differ only in the replaced characters get different folders.
    assert run_dir_name("a b") != run_dir_name("a/b")
    assert run_dir_name("..") not in ("", ".", "..")


def test_a_server_that_exits_is_reported_with_its_log(tmp_path):
    fake = tmp_path / "llama-server"
    fake.write_text("#!/bin/sh\necho 'error: model file not found'\nexit 3\n")
    fake.chmod(0o755)
    runner = LlamaCppRunner(fake, tmp_path / "work", ready_timeout=10)

    with pytest.raises(RunnerError, match="exited with code 3") as info:
        asyncio.run(runner.start(_spec()))
    assert "model file not found" in str(info.value)


def test_the_run_folder_name_is_safe(tmp_path):
    fake = tmp_path / "llama-server"
    fake.write_text("#!/bin/sh\nexit 3\n")
    fake.chmod(0o755)
    runner = LlamaCppRunner(fake, tmp_path / "work", ready_timeout=10)

    with pytest.raises(RunnerError):
        asyncio.run(runner.start(_spec(model_id="main-model (as ocr)")))
    (folder,) = (tmp_path / "work").iterdir()
    assert folder.name == run_dir_name("main-model (as ocr)")
    assert (folder / "server.log").exists()


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


FAKE_SERVER = """#!/usr/bin/env python3
# Stands in for llama-server: fails to bind the first N times, then answers /health.
import http.server, pathlib, sys
args = sys.argv[1:]
port = int(args[args.index("--port") + 1])
count_file = pathlib.Path(__file__).with_name("starts")
count = int(count_file.read_text()) + 1 if count_file.exists() else 1
count_file.write_text(str(count))
if count <= {fail_binds}:
    print("E srv start: couldn't bind HTTP server socket, port: %d" % port, flush=True)
    sys.exit(1)
if {other_failure}:
    print("error: failed to load model", flush=True)
    sys.exit(1)
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
    def log_message(self, *a): pass
http.server.HTTPServer(("127.0.0.1", port), H).serve_forever()
"""


def _fake_server(tmp_path, *, fail_binds=0, other_failure=False):
    server = tmp_path / "bin" / "llama-server"
    server.parent.mkdir()
    server.write_text(FAKE_SERVER.format(fail_binds=fail_binds, other_failure=other_failure))
    server.chmod(0o755)
    return server


def _starts(server):
    return int((server.parent / "starts").read_text())


@pytest.mark.skipif(sys.platform == "win32", reason="shebang script")
def test_a_port_taken_before_binding_is_retried_on_a_new_port(tmp_path):
    server = _fake_server(tmp_path, fail_binds=1)
    runner = LlamaCppRunner(server, tmp_path / "work", ready_timeout=20)

    async def go():
        handle = await runner.start(_spec())
        try:
            return handle.is_running()
        finally:
            await handle.stop()

    assert asyncio.run(go()) is True
    assert _starts(server) == 2


@pytest.mark.skipif(sys.platform == "win32", reason="shebang script")
def test_other_start_failures_are_not_retried(tmp_path):
    server = _fake_server(tmp_path, other_failure=True)
    runner = LlamaCppRunner(server, tmp_path / "work", ready_timeout=20)

    with pytest.raises(RunnerError, match="failed to load model"):
        asyncio.run(runner.start(_spec()))
    assert _starts(server) == 1


@pytest.mark.skipif(sys.platform == "win32", reason="shebang script")
def test_bind_failures_give_up_after_three_attempts(tmp_path):
    server = _fake_server(tmp_path, fail_binds=5)
    runner = LlamaCppRunner(server, tmp_path / "work", ready_timeout=20)

    with pytest.raises(RunnerError, match="couldn't bind"):
        asyncio.run(runner.start(_spec()))
    assert _starts(server) == 3
