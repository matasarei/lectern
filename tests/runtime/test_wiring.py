from __future__ import annotations

import pytest

from lectern.catalog import catalog
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


def test_with_mlx_on_and_neither_file_downloaded_the_build_is_the_one_asked_for(mac, monkeypatch):
    runtime, switch = mac
    switch(True)
    # The usual file only stands in while it is there. With nothing on disk MLX was asked
    # for, so the build is what's missing, and cleanup is planned for the chat on its build.
    monkeypatch.setattr(DownloadManager, "is_downloaded", lambda self, entry: False)
    with pytest.raises(NotAvailable, match=r"Qwen3.5 35B-A3B \(MLX\) isn't downloaded yet"):
        runtime.resolve("main")
    assert runtime.cleanup_default() == "qwen3.5-4b"
    assert runtime.cleanup_key() == "main@qwen3.5-4b"
    assert wiring.cleanup_model(runtime).id == "qwen3.5-4b"
    switch(False)
    with pytest.raises(NotAvailable, match=r"Qwen3.5 35B-A3B isn't downloaded yet"):
        runtime.resolve("main")
    assert runtime.cleanup_key() == "main"


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


def test_cleanup_is_kept_off_the_mlx_build_and_on_the_model_it_was_tuned_on(mac, monkeypatch):
    runtime, switch = mac
    # MLX's server can't hold a model to a JSON schema, which OCR cleanup relies on.
    assert runtime.cleanup_key() == "main"  # MLX off: the chat's own model can, and is loaded
    assert runtime.cleanup_default() == "qwen3.5-35b-a3b"
    assert wiring.cleanup_model(runtime) is None  # nothing but the chat's model to fetch
    switch(True)
    key = runtime.cleanup_key()
    # Not the chat model's own GGUF (22 GB beside its MLX build): the minimum tier's chat
    # model, the one cleanup was tuned and timed on.
    assert key == "main@qwen3.5-4b"
    assert runtime.cleanup_default() == "qwen3.5-4b"
    assert runtime.resolve(key).format == "gguf"
    assert runtime.resolve("main").format == "mlx"  # the chat itself is on MLX
    assert wiring.cleanup_model(runtime).id == "qwen3.5-4b"

    monkeypatch.setattr(
        DownloadManager, "is_downloaded", lambda self, entry: entry.format == "gguf"
    )
    assert runtime.cleanup_key() == "main"  # the build isn't there: nothing to avoid


# --- a model chosen for a role, in place of the one recommended for the computer --------------


@pytest.fixture
def card(paths, monkeypatch):
    """The runtime of an 8 GB card (tier recommended: the chat model is the 9B) with every
    model downloaded. Returns (runtime, choose): choose(role, model_id) stores a choice."""
    monkeypatch.delenv("LECTERN_BUDGET_MB", raising=False)
    engine = make_engine(paths.app_db)
    migrate(engine)
    sessions = make_sessionmaker(engine)
    monkeypatch.setattr(DownloadManager, "is_downloaded", lambda self, entry: True)
    monkeypatch.setattr(wiring, "_shape", lambda path: planner.ModelShape(8, 8, 1, 64, 64))
    monkeypatch.setattr(mlx, "read_shape", lambda folder: planner.ModelShape(8, 8, 1, 64, 64))
    runtime = build_runtime(
        paths, DownloadManager(paths.models, sessions), _hw(int(7.4 * GIB)), sessions
    )

    def choose(role: str, model_id: str | None) -> None:
        chosen = settings.get(sessions, settings.MODELS_KEY, settings.MODEL_DEFAULTS)
        settings.put(sessions, settings.MODELS_KEY, {**chosen, role: model_id})

    choose.sessions = sessions  # for the tests that switch MLX on as well
    return runtime, choose


def test_every_role_can_have_a_chosen_model():
    from typing import get_args

    from lectern.catalog.catalog import Role

    # And cleanup of scanned pages, which isn't a catalog role: a chat model does it.
    assert set(settings.MODEL_DEFAULTS) == {*get_args(Role), settings.CLEANUP}
    assert set(settings.MODEL_DEFAULTS.values()) == {None}  # nothing chosen: the recommended


def test_a_role_uses_the_model_chosen_for_it(card):
    runtime, choose = card
    assert runtime.resolve("main").model_id == "qwen3.5-9b"  # recommended for this card
    choose("main", "qwen3.5-4b")
    assert runtime.resolve("main").model_id == "qwen3.5-4b"
    assert runtime.resolve("ocr").model_id == "paddleocr-vl-1.6"  # the other roles as before
    assert wiring.role_model(runtime, "main").id == "qwen3.5-4b"
    choose("main", None)
    assert runtime.resolve("main").model_id == "qwen3.5-9b"


@pytest.mark.parametrize(
    "stored",
    [
        "gone-from-the-catalog",
        "paddleocr-vl-1.6",  # in the catalog, not a chat model
        "qwen3.5-4b-mlx",  # a build belongs to its model: never a role's own choice
    ],
)
def test_a_stored_choice_that_cannot_be_used_falls_back_to_the_recommended(card, stored):
    runtime, choose = card
    choose("main", stored)
    assert runtime.resolve("main").model_id == "qwen3.5-9b"


def test_a_key_that_names_a_model_is_not_the_roles_choice(card):
    runtime, choose = card
    choose("ocr.hand", "qwen3-vl-4b")
    assert runtime.resolve("ocr.hand@chandra-ocr-2").model_id == "chandra-ocr-2"
    assert runtime.resolve("ocr.hand").model_id == "qwen3-vl-4b"


def test_the_roles_model_is_none_for_a_role_nothing_fills(card):
    runtime, _ = card
    assert wiring.role_model(runtime, "stt") is None
    assert wiring.role_model(runtime, "code").id == "qwen2.5-coder-7b"


def test_with_mlx_on_the_chosen_models_build_runs_and_cleanup_names_the_chosen_gguf(
    card, monkeypatch
):
    runtime, choose = card
    monkeypatch.setattr(mlx, "mlx_supported", lambda: True)
    monkeypatch.setattr(mlx, "mlx_available", lambda: True)
    settings.put(choose.sessions, settings.RUNTIME_KEY, {"mlx_enabled": True})
    choose("main", "qwen3.5-4b")
    assert runtime.resolve("main").model_id == "qwen3.5-4b-mlx"
    assert runtime.cleanup_key() == "main@qwen3.5-4b"
    choose("main", "qwen3.5-9b")  # whatever the chat model, cleanup stays on the small one
    assert runtime.resolve("main").model_id == "qwen3.5-9b-mlx"
    assert runtime.cleanup_key() == "main@qwen3.5-4b"


def test_a_model_chosen_for_cleanup_cleans_on_llama_cpp(card):
    runtime, choose = card  # the chat is on the 9B's GGUF
    choose("clean", "qwen3.5-4b")
    assert runtime.cleanup_key() == "main@qwen3.5-4b"
    assert wiring.cleans_pages(runtime).id == "qwen3.5-4b"
    assert wiring.cleanup_model(runtime).id == "qwen3.5-4b"
    assert runtime.cleanup_default() == "qwen3.5-9b"  # what it would be, left alone

    choose("clean", "qwen3.5-9b")  # the chat's own model: its loaded copy is used
    assert runtime.cleanup_key() == "main"
    assert wiring.cleans_pages(runtime).id == "qwen3.5-9b"
    assert wiring.cleanup_model(runtime) is None


def test_a_model_chosen_for_cleanup_cleans_with_the_chat_on_mlx(card, monkeypatch):
    runtime, choose = card
    monkeypatch.setattr(mlx, "mlx_supported", lambda: True)
    monkeypatch.setattr(mlx, "mlx_available", lambda: True)
    settings.put(choose.sessions, settings.RUNTIME_KEY, {"mlx_enabled": True})
    choose("clean", "qwen3.5-9b")
    assert runtime.resolve("main").model_id == "qwen3.5-9b-mlx"
    # The chat's own model, but not the copy the chat runs on: that one is the MLX build.
    assert runtime.cleanup_key() == "main@qwen3.5-9b"
    assert runtime.cleanup_default() == "qwen3.5-4b"


def test_without_a_chat_model_in_the_catalog_nothing_is_named_for_cleanup(card, monkeypatch):
    runtime, _ = card
    real = catalog.load()
    no_chat = [m for m in real.models if "main" not in m.roles]
    readers = real.model_copy(update={"models": no_chat})
    monkeypatch.setattr(catalog, "load", lambda: readers)

    assert runtime.cleanup_default() is None
    assert runtime.cleanup_key() == "main"  # leasing it says there's no chat model
    assert wiring.cleanup_model(runtime) is None


@pytest.mark.parametrize(
    "stored",
    [
        "gone-from-the-catalog",
        "paddleocr-vl-1.6",  # in the catalog, not a chat model
        "qwen3.5-4b-mlx",  # an MLX build can't be held to cleanup's schema
    ],
)
def test_a_cleanup_choice_that_cannot_be_used_falls_back_to_the_automatic_one(card, stored):
    runtime, choose = card
    choose("clean", stored)
    assert runtime.cleanup_key() == "main"
    assert wiring.cleans_pages(runtime).id == "qwen3.5-9b"


# --- after a change of hardware, a role nobody set keeps the model it has ---------------------


@pytest.fixture
def started(paths, monkeypatch):
    """started(tier, on_disk) builds the runtime as a start on a computer of that tier would,
    over one database, with only the models in `on_disk` downloaded. Returns
    (runtime, the stored choices)."""
    monkeypatch.delenv("LECTERN_BUDGET_MB", raising=False)
    engine = make_engine(paths.app_db)
    migrate(engine)
    sessions = make_sessionmaker(engine)
    monkeypatch.setattr(wiring, "_shape", lambda path: planner.ModelShape(8, 8, 1, 64, 64))

    def start(tier: str, on_disk: set[str]):
        monkeypatch.setenv("LECTERN_TIER", tier)
        monkeypatch.setattr(
            DownloadManager, "is_downloaded", lambda self, entry: entry.id in on_disk
        )
        runtime = build_runtime(
            paths, DownloadManager(paths.models, sessions), _hw(int(7.4 * GIB)), sessions
        )
        return runtime, settings.get(sessions, settings.MODELS_KEY, settings.MODEL_DEFAULTS)

    start.sessions = sessions
    return start


def test_a_role_keeps_its_installed_model_when_the_computer_moves_up_a_tier(started):
    runtime, chosen = started("minimum", {"qwen3.5-4b"})
    assert chosen["main"] is None  # the recommended model is the one on disk: nothing to keep
    assert runtime.resolve("main").model_id == "qwen3.5-4b"

    # A new video card: the 9B is recommended now, and it isn't downloaded.
    runtime, chosen = started("recommended", {"qwen3.5-4b"})
    assert chosen["main"] == "qwen3.5-4b"
    assert runtime.resolve("main").model_id == "qwen3.5-4b"  # the chat goes on working
    assert chosen["code"] is None  # a role with nothing on disk follows the recommendation

    # It's a choice from here on: later starts, and the 9B arriving, change nothing.
    _, chosen = started("recommended", {"qwen3.5-4b", "qwen3.5-9b"})
    assert chosen["main"] == "qwen3.5-4b"


def test_a_fresh_install_stores_nothing_and_asks_for_the_recommended_model(started):
    runtime, chosen = started("recommended", set())
    assert set(chosen.values()) == {None}
    with pytest.raises(NotAvailable, match="Qwen3.5 9B isn't downloaded yet"):
        runtime.resolve("main")


def test_of_several_installed_models_the_largest_is_kept(started):
    on_disk = {"qwen3.5-4b", "qwen3.5-9b", "qwen3.5-9b-mlx"}
    started("recommended", on_disk)
    _, chosen = started("comfortable", on_disk)
    assert chosen["main"] == "qwen3.5-9b"  # and never an MLX build


def test_on_the_same_computer_a_role_set_back_to_the_recommended_model_stays_there(started):
    """The 9B was chosen and downloaded, then "Use it" put the role back on the recommended
    35B, which stores no choice. Starting again before the 35B is downloaded mustn't undo
    that: nothing about the computer changed."""
    for _ in range(2):
        runtime, chosen = started("comfortable", {"qwen3.5-9b"})
        assert chosen["main"] is None
        with pytest.raises(NotAvailable, match="Qwen3.5 35B-A3B isn't downloaded yet"):
            runtime.resolve("main")


def test_the_first_start_only_notes_the_computer_it_runs_on(started):
    # Nothing to compare with yet, so nothing is taken to have changed.
    _, chosen = started("comfortable", {"qwen3.5-9b"})
    assert set(chosen.values()) == {None}
    seen = settings.get(started.sessions, settings.HARDWARE_KEY, settings.HARDWARE_DEFAULTS)
    assert seen == {"tier": "comfortable"}


def test_a_restricted_licence_model_never_becomes_a_roles_model_by_itself(started):
    """Chandra is on disk because a page was once re-read with it by name. A default must be
    permissively licensed, and so must a model a role takes without being asked."""
    started("minimum", {"chandra-ocr-2"})
    _, chosen = started("recommended", {"chandra-ocr-2"})
    assert chosen["ocr.hand"] is None

    _, chosen = started("comfortable", {"chandra-ocr-2", "qwen3-vl-4b"})
    assert chosen["ocr.hand"] == "qwen3-vl-4b"  # the permissive one that's there


def test_a_model_this_computer_is_not_meant_for_is_not_kept(started):
    # The data moved to a smaller computer: the 35B is on disk and not listed for this tier.
    started("comfortable", {"qwen3.5-35b-a3b"})
    runtime, chosen = started("minimum", {"qwen3.5-35b-a3b"})
    assert chosen["main"] is None
    with pytest.raises(NotAvailable, match="Qwen3.5 4B isn't downloaded yet"):
        runtime.resolve("main")


def test_a_role_that_was_given_a_model_is_left_alone(started):
    stored = {**settings.MODEL_DEFAULTS, "main": "qwen3.5-35b-a3b"}
    settings.put(started.sessions, settings.MODELS_KEY, stored)
    started("minimum", {"qwen3.5-4b"})
    _, chosen = started("recommended", {"qwen3.5-4b"})
    assert chosen["main"] == "qwen3.5-35b-a3b"  # the person's choice, downloaded or not
