from __future__ import annotations

import pytest

from lectern.core import settings
from lectern.core.db import make_engine, make_sessionmaker, migrate
from lectern.downloads.manager import DownloadManager
from lectern.hardware.probe import GIB, Gpu, HardwareInfo
from lectern.runners import mlx
from lectern.runtime import planner, wiring
from lectern.runtime.supervisor import NotAvailable
from lectern.runtime.wiring import CPU_RESERVE, budget_for, build_runtime

MIB = 1024**2


def _hw(gpu_budget=0):
    gpus = (
        [Gpu("nvidia", "RTX", 8 * GIB, 8 * GIB, unified=False, cuda_driver="13.0")]
        if gpu_budget
        else []
    )
    return HardwareInfo(
        os="linux",
        arch="x86_64",
        cpu="c",
        cpu_cores=8,
        ram_total=16 * GIB,
        ram_available=8 * GIB,
        gpus=gpus,
        gpu_budget=gpu_budget,
    )


def test_gpu_budget_by_default(monkeypatch):
    monkeypatch.delenv("LECTERN_BUDGET_MB", raising=False)
    assert budget_for(_hw(int(7.4 * GIB))) == (int(7.4 * GIB), "gpu")


def test_cpu_only_uses_ram_minus_the_reserve(monkeypatch):
    monkeypatch.delenv("LECTERN_BUDGET_MB", raising=False)
    assert budget_for(_hw()) == (16 * GIB - CPU_RESERVE, "ram")


def test_env_override(monkeypatch):
    monkeypatch.setenv("LECTERN_BUDGET_MB", "5000")
    assert budget_for(_hw(int(7.4 * GIB))) == (5000 * MIB, "LECTERN_BUDGET_MB")


@pytest.mark.parametrize("bad", ["abc", "-5", "0", "1.5", " "])
def test_bad_env_override_says_what_is_wrong(monkeypatch, bad):
    monkeypatch.setenv("LECTERN_BUDGET_MB", bad)
    with pytest.raises(ValueError, match="LECTERN_BUDGET_MB must be a whole number"):
        budget_for(_hw())


@pytest.fixture
def resolve(paths, monkeypatch):
    """The real runtime's resolve on an 8 GB card, with every model 'downloaded' and no
    GGUF read from disk."""
    monkeypatch.delenv("LECTERN_BUDGET_MB", raising=False)
    engine = make_engine(paths.app_db)
    migrate(engine)
    sessions = make_sessionmaker(engine)
    downloads = DownloadManager(paths.models, sessions)
    monkeypatch.setattr(DownloadManager, "is_downloaded", lambda self, entry: True)
    monkeypatch.setattr(wiring, "_shape", lambda path: planner.ModelShape(8, 8, 1, 64, 64))
    return build_runtime(paths, downloads, _hw(int(7.4 * GIB)), sessions).resolve


def test_a_role_gets_the_tiers_default(resolve):
    choice = resolve("ocr")
    assert (choice.role, choice.model_id) == ("ocr", "paddleocr-vl-1.6")
    assert choice.mmproj is not None


def test_a_role_and_model_gets_that_model(resolve):
    choice = resolve("ocr.hand@chandra-ocr-2")
    # The role stays plain: the vision check for MLX runners looks at it.
    assert (choice.role, choice.model_id) == ("ocr.hand", "chandra-ocr-2")
    assert choice.tier_ctx == 4096


@pytest.mark.parametrize(
    ("key", "message"),
    [
        ("ocr@nope", "There's no model 'nope' in the catalog"),
        ("ocr@qwen3-vl-4b", "Qwen3-VL 4B Instruct can't be used for the 'ocr' role"),
        ("stt", "No model is set for the 'stt' role"),
    ],
)
def test_a_key_that_names_nothing_usable_says_why(resolve, key, message):
    with pytest.raises(NotAvailable, match=message):
        resolve(key)


def test_a_chosen_model_must_be_downloaded(resolve, monkeypatch):
    monkeypatch.setattr(DownloadManager, "is_downloaded", lambda self, entry: False)
    with pytest.raises(
        NotAvailable, match="Chandra OCR 2 isn't downloaded yet. Download it in Models"
    ):
        resolve("ocr.hand@chandra-ocr-2")


def test_the_engines_download_progress_reaches_the_runtime(paths, monkeypatch):
    from lectern.runners.llamacpp import InstallingLlamaCppRunner

    monkeypatch.delenv("LECTERN_BUDGET_MB", raising=False)
    engine = make_engine(paths.app_db)
    migrate(engine)
    sessions = make_sessionmaker(engine)
    monkeypatch.setattr(DownloadManager, "is_downloaded", lambda self, entry: True)
    monkeypatch.setattr(wiring, "_shape", lambda path: planner.ModelShape(8, 8, 1, 64, 64))
    runtime = build_runtime(paths, DownloadManager(paths.models, sessions),
                            _hw(int(7.4 * GIB)), sessions)  # fmt: skip

    runner = runtime.runner_for(runtime.resolve("main"))
    assert isinstance(runner, InstallingLlamaCppRunner)
    runner.on_progress(runner.variant, 5, 50)
    assert runtime.state()["activity"] == {
        "kind": "engine",
        "variant": runner.variant,
        "done": 5,
        "total": 50,
    }


# --- on a Mac: a model's MLX build in its place, when MLX is on and the build is there --------


@pytest.fixture
def mac(paths, monkeypatch):
    """The runtime of an Apple Silicon Mac with MLX installed and every model downloaded.
    Returns (runtime, switch): switch(True) turns MLX on in the settings."""
    monkeypatch.delenv("LECTERN_BUDGET_MB", raising=False)
    engine = make_engine(paths.app_db)
    migrate(engine)
    sessions = make_sessionmaker(engine)
    monkeypatch.setattr(DownloadManager, "is_downloaded", lambda self, entry: True)
    monkeypatch.setattr(wiring, "_shape", lambda path: planner.ModelShape(8, 8, 1, 64, 64))
    monkeypatch.setattr(mlx, "read_shape", lambda folder: planner.ModelShape(8, 8, 1, 64, 64))
    monkeypatch.setattr(mlx, "mlx_supported", lambda: True)
    monkeypatch.setattr(mlx, "mlx_available", lambda: True)
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
    runtime = build_runtime(paths, DownloadManager(paths.models, sessions), hw, sessions)

    def switch(on: bool) -> None:
        settings.put(sessions, settings.RUNTIME_KEY, {"mlx_enabled": on})

    return runtime, switch


def test_with_mlx_on_a_role_gets_its_models_mlx_build(mac, paths):
    runtime, switch = mac
    switch(True)
    choice = runtime.resolve("main")
    assert (choice.role, choice.model_id, choice.format) == ("main", "qwen3.5-35b-a3b-mlx", "mlx")
    assert choice.model_file == paths.models / "qwen3.5-35b-a3b-mlx"  # the folder, not a file
    assert choice.mmproj is None
    runner = runtime.runner_for(choice)
    assert isinstance(runner, mlx.MlxRunner) and not runner.vision


def test_mlx_is_a_choice_and_off_until_switched_on(mac):
    runtime, switch = mac
    assert runtime.resolve("main").model_id == "qwen3.5-35b-a3b"  # nothing was switched
    switch(True)
    switch(False)
    assert runtime.resolve("main").model_id == "qwen3.5-35b-a3b"


def test_until_the_mlx_build_is_downloaded_the_gguf_runs(mac, monkeypatch):
    runtime, switch = mac
    switch(True)
    # Turning the switch on never breaks a chat: nothing changes until the build is there.
    monkeypatch.setattr(
        DownloadManager, "is_downloaded", lambda self, entry: entry.format == "gguf"
    )
    choice = runtime.resolve("main")
    assert (choice.model_id, choice.format) == ("qwen3.5-35b-a3b", "gguf")


@pytest.mark.parametrize("missing", ["mlx_available", "mlx_supported"])
def test_without_mlx_on_this_machine_the_switch_changes_nothing(mac, monkeypatch, missing):
    runtime, switch = mac
    switch(True)  # a setting carried over from another machine, say
    monkeypatch.setattr(mlx, "mlx_available", lambda: False)  # false off Apple Silicon too
    monkeypatch.setattr(mlx, missing, lambda: False)
    assert runtime.resolve("main").model_id == "qwen3.5-35b-a3b"


def test_a_named_model_is_what_runs_whatever_the_switch_says(mac):
    runtime, switch = mac
    switch(True)
    assert runtime.resolve("main@qwen3.5-35b-a3b").format == "gguf"
    assert runtime.resolve("main@qwen3.5-4b").model_id == "qwen3.5-4b"
    switch(False)
    # Asked for by name, an MLX build is resolved; whether it may run is runner_for's answer.
    named = runtime.resolve("main@qwen3.5-4b-mlx")
    assert named.format == "mlx"
    with pytest.raises(NotAvailable, match="MLX is off"):
        runtime.runner_for(named)


def test_a_role_whose_model_has_no_mlx_build_is_unchanged(mac):
    runtime, switch = mac
    switch(True)
    assert runtime.resolve("ocr").format == "gguf"
    assert runtime.resolve("code").format == "gguf"
