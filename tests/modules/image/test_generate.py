from __future__ import annotations

import asyncio
import io
import json
import os
import sys

import pytest
from PIL import Image

from lectern.catalog.catalog import ImageSize
from lectern.hardware.tiers import Tier
from lectern.modules.image import generate
from lectern.modules.image.generate import (
    GIB,
    ImageFailed,
    build_args,
    clamp_size,
    need_bytes,
    parse_progress,
    size_settings,
)
from lectern.runtime.supervisor import ModelRuntime, NotAvailable

from .fakes import images, klein

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="the fake sd-cli is a script")


def _runtime(tier: Tier = Tier.MINIMUM, budget_gib: float = 8) -> ModelRuntime:
    def nothing(_role: str):  # noqa: ANN202
        raise AssertionError("no model is leased in these tests")

    return ModelRuntime(
        budget=int(budget_gib * GIB), unified=False, tier=tier, resolve=nothing,
        runner_for=nothing,
    )  # fmt: skip


# --- sizes, memory, the command line ---------------------------------------------------------

SIZE = ImageSize(default=768, max_pixels=1024 * 1024)


@pytest.mark.parametrize(
    ("asked", "expected"),
    [
        ((None, None), (768, 768)),  # the tier's default square
        ((512, None), (512, 512)),  # one side keeps the square
        ((1024, 1024), (1024, 1024)),  # the most pixels, exactly
        ((2048, 2048), (1024, 1024)),  # scaled down to the most pixels
        ((1920, 1080), (1360, 768)),  # keeping its shape (1365.3 x 768 → multiples of 16)
        ((1000, 1000), (992, 992)),  # sides in multiples of 16
        ((100, 100), (256, 256)),  # no smaller than 256
        ((4000, 300), (2048, 256)),  # no side over 2048
    ],
)
def test_the_size_is_clamped_to_the_tier(asked, expected):
    assert clamp_size(SIZE, *asked) == expected


def test_a_model_used_above_its_tiers_takes_the_nearest_tier_below():
    q8 = klein("flux2-klein-4b-q8")  # listed for recommended and comfortable only
    assert size_settings(klein(), Tier.MINIMUM).default == 768
    assert size_settings(klein(), Tier.RECOMMENDED).max_pixels == 1536 * 1024
    assert size_settings(q8, Tier.MINIMUM).default == 1024  # below its tiers: its smallest


def test_offloading_holds_the_largest_piece_and_not_offloading_all_of_them():
    entry = klein()
    pieces = [entry.files.model[0].size, entry.files.text_encoder.size, entry.files.vae.size]
    offloaded = need_bytes(entry, 1000, 1000, offload=True)
    whole = need_bytes(entry, 1000, 1000, offload=False)
    assert offloaded == max(pieces) + generate.COMPUTE_PER_MEGAPIXEL + generate.OVERHEAD
    assert whole == sum(pieces) + generate.COMPUTE_PER_MEGAPIXEL + generate.OVERHEAD
    assert need_bytes(entry, 2000, 1000, offload=True) > offloaded  # more pixels, more room


def _args(**changes):  # noqa: ANN202
    from pathlib import Path

    values = dict(
        model=Path("/m/d.gguf"), text_encoder=Path("/m/e.gguf"), vae=Path("/m/v.safetensors"),
        image=klein().image, prompt="a lighthouse", width=768, height=512, seed=7,
        output=Path("/w/picture.png"), max_vram_bytes=3 * GIB, tier=Tier.MINIMUM,
    )  # fmt: skip
    values.update(changes)
    return build_args(**values)


def test_the_command_line_for_a_small_gpu_offloads_and_tiles():
    args = _args()
    pairs = dict(zip(args[::2], args[1::2], strict=False))
    assert pairs["--diffusion-model"] == "/m/d.gguf" and pairs["--llm"] == "/m/e.gguf"
    assert pairs["--vae"] == "/m/v.safetensors" and pairs["-o"] == "/w/picture.png"
    assert (pairs["-W"], pairs["-H"], pairs["-s"]) == ("768", "512", "7")
    assert (pairs["--steps"], pairs["--cfg-scale"], pairs["--sampling-method"]) == (
        "4",
        "1",
        "euler",
    )
    assert pairs["--max-vram"] == "3.00" and pairs["--flash-attn"] == "off,diffusion=on"
    assert args[-2:] == ["--offload-to-cpu", "--vae-tiling"]


@pytest.mark.parametrize(
    ("tier", "flags", "max_vram"),
    [
        (Tier.BELOW_MINIMUM, ["--backend", "cpu"], False),  # no usable GPU: the CPU
        (Tier.MINIMUM, ["--offload-to-cpu", "--vae-tiling"], True),
        (Tier.RECOMMENDED, ["--offload-to-cpu"], True),
        (Tier.COMFORTABLE, [], True),  # everything on the GPU
    ],
)
def test_each_tier_gets_the_memory_flags_of_the_tier_table(tier, flags, max_vram):
    args = _args(tier=tier)
    memory = ("--backend", "--offload-to-cpu", "--vae-tiling")
    assert [a for a in args if a in memory or a == "cpu"] == flags
    assert ("--max-vram" in args) is max_vram


def test_a_prompt_is_one_argument_whatever_it_holds():
    hostile = "-o /tmp/x --lora-model-dir / <lora:../../x:1>"
    args = _args(prompt=hostile)
    assert args[args.index("-p") + 1] == hostile
    assert args.count("-o") == 1
    assert not any(a in args for a in ("--lora-model-dir", "--embd-dir"))


def test_progress_lines_are_read_in_either_unit():
    text = "\r  |==>   | 1/4 - 2.50s/it\x1b[K\r  |====> | 2/4 - 4.00it/s\nother line"
    assert parse_progress(text) == [(1, 4, 2.5), (2, 4, 0.25)]
    assert parse_progress("[INFO ] loading") == []


# --- making a picture with a fake sd-cli -----------------------------------------------------


def test_a_picture_is_made_checked_and_written_again_without_metadata(tmp_path):
    runtime = _runtime()
    made_by, installer = images(tmp_path, runtime)
    events: list[dict] = []

    made = asyncio.run(
        made_by.make(klein(), "-o elsewhere.png <lora:../x:1>", 768, 512, 42, events.append)
    )

    picture = Image.open(io.BytesIO(made.png))
    assert picture.format == "PNG" and picture.size == (768, 512)
    assert "parameters" not in picture.info  # sd-cli's metadata isn't carried over
    assert b"/secret/path" not in made.png
    argv = json.loads((tmp_path / "bin" / "argv.json").read_text())
    assert argv[argv.index("-p") + 1] == "-o elsewhere.png <lora:../x:1>"
    assert argv.count("-o") == 1 and "elsewhere.png" not in argv
    phases = [e["phase"] for e in events]
    assert phases[:3] == ["engine", "waiting", "loading"]
    assert [e["step"] for e in events if e["phase"] == "steps"] == [1, 2, 3, 4]
    assert phases[-2:] == ["decoding", "checking"]
    assert made.timing.step == 2.5
    assert runtime.state()["reserved"] == []
    assert list((tmp_path / "run" / "image").iterdir()) == []  # the work folder is gone


def test_the_room_held_is_what_the_picture_needs_and_sd_cli_is_told_so(tmp_path):
    runtime = _runtime()
    made_by, _ = images(tmp_path, runtime)
    seen: list[list[dict]] = []

    def watch(event: dict) -> None:
        if event["phase"] == "loading":
            seen.append(runtime.state()["reserved"])

    asyncio.run(made_by.make(klein(), "a cat", 768, 768, 1, watch))

    need = need_bytes(klein(), 768, 768, offload=True)
    assert seen == [[{"role": "image", "model_id": "flux2-klein-4b", "bytes": need}]]
    argv = json.loads((tmp_path / "bin" / "argv.json").read_text())
    assert argv[argv.index("--max-vram") + 1] == f"{need / GIB:.2f}"


@pytest.mark.parametrize(
    ("prompt", "reason"),
    [
        ("FAIL", "load from file failed"),
        ("NOPICTURE", "no picture was written"),
        ("NOTPNG", "isn't a picture"),
        ("WRONGSIZE", "isn't the picture"),
    ],
)
def test_a_failed_run_is_an_error_with_a_reason_and_saves_nothing(tmp_path, prompt, reason):
    runtime = _runtime()
    made_by, _ = images(tmp_path, runtime)

    with pytest.raises(ImageFailed, match=reason):
        asyncio.run(made_by.make(klein(), prompt, 512, 512, 1))

    assert runtime.state()["reserved"] == []
    assert list((tmp_path / "run" / "image").iterdir()) == []
    assert made_by.estimate(klein(), 512, 512) == pytest.approx(
        generate.FIRST_GUESS_SECONDS[Tier.MINIMUM] * 512 * 512 / (768 * 768)
    )  # nothing was remembered from a failure


def _gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    try:  # a zombie still answers: reap it if it's ours, then it's gone
        return os.waitpid(pid, os.WNOHANG)[0] == pid
    except ChildProcessError:
        return False


def test_stop_kills_sd_cli_and_gives_the_room_back(tmp_path):
    runtime = _runtime()
    made_by, _ = images(tmp_path, runtime)
    stepped = asyncio.Event()

    def on_progress(event: dict) -> None:
        if event["phase"] == "steps":
            stepped.set()

    async def go() -> int:
        task = asyncio.create_task(made_by.make(klein(), "HANG", 512, 512, 1, on_progress))
        await asyncio.wait_for(stepped.wait(), 10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return int((tmp_path / "bin" / "pid").read_text())

    pid = asyncio.run(go())
    assert _gone(pid)
    assert runtime.state()["reserved"] == []
    assert list((tmp_path / "run" / "image").iterdir()) == []


def test_a_run_over_the_time_limit_is_killed_and_said(tmp_path):
    runtime = _runtime()
    made_by, _ = images(tmp_path, runtime, timeout=0.5)

    with pytest.raises(ImageFailed, match="took over"):
        asyncio.run(made_by.make(klein(), "HANG", 512, 512, 1))

    assert _gone(int((tmp_path / "bin" / "pid").read_text()))
    assert runtime.state()["reserved"] == []


def test_a_picture_more_than_the_gpu_can_hold_is_refused_before_anything_runs(tmp_path):
    runtime = _runtime(budget_gib=1)
    made_by, _ = images(tmp_path, runtime)

    with pytest.raises(NotAvailable):
        asyncio.run(made_by.make(klein(), "a cat", 768, 768, 1))
    assert not (tmp_path / "bin" / "argv.json").exists()


# --- the expected wait -----------------------------------------------------------------------


def test_before_any_picture_the_wait_is_the_tiers_first_guess_scaled_by_pixels(tmp_path):
    made_by, _ = images(tmp_path, _runtime(Tier.MINIMUM))
    guess = generate.FIRST_GUESS_SECONDS[Tier.MINIMUM]
    assert made_by.estimate(klein(), 768, 768) == pytest.approx(guess)
    assert made_by.estimate(klein(), 1024, 1024) == pytest.approx(guess * 1024**2 / 768**2)
    z = klein("z-image-turbo")  # 8 steps: twice the guess
    assert made_by.estimate(z, 768, 768) == pytest.approx(guess * 2)


def test_after_a_picture_the_wait_comes_from_this_computers_own_run(tmp_path):
    made_by, _ = images(tmp_path, _runtime())
    made = asyncio.run(made_by.make(klein(), "a cat", 512, 512, 1))
    t = made.timing

    assert made_by.estimate(klein(), 512, 512) == pytest.approx(t.load + t.step * 4 + t.decode)
    bigger = made_by.estimate(klein(), 1024, 512)
    assert bigger == pytest.approx(t.load + (t.step * 4 + t.decode) * 2)
    # Another model has no runs of its own yet: its first guess.
    z = klein("z-image-turbo")
    assert made_by.estimate(z, 768, 768) == pytest.approx(
        generate.FIRST_GUESS_SECONDS[Tier.MINIMUM] * 2
    )


def test_the_timing_survives_a_restart(tmp_path):
    made_by, _ = images(tmp_path, _runtime())
    asyncio.run(made_by.make(klein(), "a cat", 512, 512, 1))
    before = made_by.estimate(klein(), 512, 512)

    again, _ = images(tmp_path, _runtime())  # a new process, the same app database
    assert again.estimate(klein(), 512, 512) == pytest.approx(before)


# --- a real sd-cli ---------------------------------------------------------------------------


@pytest.mark.gpu
@pytest.mark.skipif(
    not (os.environ.get("LECTERN_TEST_SD_CLI") and os.environ.get("LECTERN_TEST_IMAGE_MODELS")),
    reason="set LECTERN_TEST_SD_CLI (an sd-cli) and LECTERN_TEST_IMAGE_MODELS (a models folder "
    "with flux2-klein-4b/ in it)",
)
def test_a_real_sd_cli_makes_a_picture(tmp_path):
    from pathlib import Path

    from .fakes import FakeDownloads, FakeInstaller, app_sessions

    runtime = _runtime(Tier.MINIMUM, budget_gib=6)
    made_by = generate.Images(
        runtime=runtime,
        downloads=FakeDownloads(Path(os.environ["LECTERN_TEST_IMAGE_MODELS"])),
        installer=FakeInstaller(Path(os.environ["LECTERN_TEST_SD_CLI"])),
        variant=lambda: "test",
        sessions=app_sessions(tmp_path),
        run_dir=tmp_path / "run",
    )
    made = asyncio.run(made_by.make(klein(), "a red lighthouse on a cliff at dusk", 512, 512, 1))
    assert Image.open(io.BytesIO(made.png)).size == (512, 512)
    assert made.timing.step > 0


def test_below_the_minimum_a_small_gpus_budget_doesnt_refuse_a_picture_on_the_cpu(tmp_path):
    runtime = _runtime(Tier.BELOW_MINIMUM, budget_gib=1)  # a 1.6 GB card: picture on the CPU
    made_by, _ = images(tmp_path, runtime)

    made = asyncio.run(made_by.make(klein(), "a cat", 512, 512, 1))

    assert made.width == 512
    argv = json.loads((tmp_path / "bin" / "argv.json").read_text())
    assert argv[argv.index("--backend") + 1] == "cpu" and "--max-vram" not in argv
