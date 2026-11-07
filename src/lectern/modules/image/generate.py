"""Making a picture: stable-diffusion.cpp's `sd-cli`, run once per image inside room the
runtime holds for it (`ModelRuntime.reserve`).

One process per image: it has no port, so nothing to protect with a key, and its memory comes
back when it exits. The command line is built in one place (`build_args`), as a list and never
through a shell; the prompt is one argument after `-p`, which sd-cli takes whole whatever it
starts with. Neither a LoRA folder nor an embeddings folder is passed, so `<lora:…>` and
embedding names in a prompt are only text. The picture is checked to be a PNG of the size asked
and written again without its metadata (sd-cli embeds the generation parameters) before it
becomes a project file.

How long each part took is kept per model and size (ImageTimingRow), and the card's expected
wait is worked out from it; before the first picture, from a first guess per tier.
"""

from __future__ import annotations

import asyncio
import io
import logging
import re
import shutil
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from lectern.catalog.catalog import IMAGE_ROLE, CatalogEntry, ImageSettings, ImageSize
from lectern.core.tables import ImageTimingRow
from lectern.hardware.tiers import Tier
from lectern.runtime.supervisor import GIB, ModelRuntime, Priority

log = logging.getLogger(__name__)

MIB = 1024**2
SIDE_STEP = 16  # sd.cpp's models want sides in multiples of 16
MIN_SIDE = 256
MAX_SIDE = 2048
# Working memory beside the weights, for a picture of a million pixels, and fixed overhead.
# A first estimate: the measurements on 4 GB and 8 GB machines replace it
# (docs/acceptance/image.md).
COMPUTE_PER_MEGAPIXEL = 1 * GIB
OVERHEAD = 300 * MIB
TIMEOUT = 30 * 60.0  # seconds: a CPU-only machine at its largest size, with room to spare
LOG_TAIL = 2000  # characters of sd-cli's output kept to explain a failure
# Seconds for one picture at the tier's default size with 4 steps, before this computer has
# made one: rough figures scaled from FLUX.2 klein 4B on an RTX 4090 (about 1 s at 1024²) and
# mflux on an M1 Max (30-40 s), until measured (docs/acceptance/image.md).
FIRST_GUESS_SECONDS = {
    Tier.BELOW_MINIMUM: 600.0,
    Tier.MINIMUM: 90.0,
    Tier.RECOMMENDED: 20.0,
    Tier.COMFORTABLE: 10.0,
}
GUESS_STEPS = 4

# sd-cli's progress line: "\r  |=====>     | 2/4 - 3.20s/it" (src/core/util.cpp).
_PROGRESS = re.compile(r"\|\s*(\d+)/(\d+) - +([\d.]+)(s/it|it/s)")
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


class ImageFailed(Exception):
    """sd-cli ended without a usable picture; the message says why, for the chat."""


@dataclass(frozen=True)
class Timing:
    load: float
    step: float
    decode: float


@dataclass(frozen=True)
class Made:
    png: bytes  # the picture, written again without metadata
    width: int
    height: int
    seed: int
    timing: Timing


# --- sizes, memory and the command line ------------------------------------------------------


def size_settings(entry: CatalogEntry, tier: Tier) -> ImageSize:
    """The model's sizes on this tier, or on the nearest tier it lists below it (a model is
    usable above what it was listed for), else the smallest it lists."""
    image = _image(entry)
    order = list(Tier)
    for candidate in reversed(order[: order.index(tier) + 1]):
        if candidate in image.sizes:
            return image.sizes[candidate]
    return min(image.sizes.values(), key=lambda s: s.max_pixels)


def clamp_size(size: ImageSize, width: int | None, height: int | None) -> tuple[int, int]:
    """The picture's size: the tier's default square when none is asked; one side given keeps
    the square; scaled down, keeping the shape, to the tier's most pixels; in multiples of 16
    between 256 and 2048."""
    w = float(width or height or size.default)
    h = float(height or width or size.default)
    scale = min(1.0, (size.max_pixels / (w * h)) ** 0.5, MAX_SIDE / w, MAX_SIDE / h)
    w, h = w * scale, h * scale

    def side(value: float) -> int:
        return max(MIN_SIDE, int(value) // SIDE_STEP * SIDE_STEP)

    return side(w), side(h)


def offloads(tier: Tier) -> bool:
    """Weights wait in RAM and come to the GPU when each part runs, below the comfortable tier:
    the pieces together are more than a 4 GB or 8 GB card holds beside the picture."""
    return tier is not Tier.COMFORTABLE


def need_bytes(entry: CatalogEntry, width: int, height: int, *, offload: bool) -> int:
    """GPU memory to hold for a picture. Offloading, the largest piece is on the GPU at a time
    (the text encoder, then the diffusion model, then the VAE); otherwise all three. Plus the
    working memory for its pixels."""
    files = entry.files
    pieces = [files.model[0].size, files.text_encoder.size, files.vae.size]  # type: ignore[union-attr]
    weights = max(pieces) if offload else sum(pieces)
    compute = int(COMPUTE_PER_MEGAPIXEL * width * height / 1_000_000)
    return weights + compute + OVERHEAD


def build_args(
    *,
    model: Path,
    text_encoder: Path,
    vae: Path,
    image: ImageSettings,
    prompt: str,
    width: int,
    height: int,
    seed: int,
    output: Path,
    max_vram_bytes: int,
    offload: bool,
) -> list[str]:
    """sd-cli's arguments, after the program. Never through a shell."""
    args = [
        "--diffusion-model", str(model),
        "--llm", str(text_encoder),
        "--vae", str(vae),
        "-p", prompt,
        "-W", str(width),
        "-H", str(height),
        "--steps", str(image.steps),
        "--cfg-scale", f"{image.cfg:g}",
        "--sampling-method", image.sampler,
        "-s", str(seed),
        "-o", str(output),
        "--flash-attn", "off,diffusion=on",
        "--max-vram", f"{max_vram_bytes / GIB:.2f}",
    ]  # fmt: skip
    if offload:
        args += ["--offload-to-cpu", "--vae-tiling"]
    return args


def parse_progress(text: str) -> list[tuple[int, int, float]]:
    """(step, steps, seconds a step) for each progress line in some of sd-cli's output."""
    found = []
    for line in re.split(r"[\r\n]", text):
        match = _PROGRESS.search(line)
        if match:
            step, total, speed, unit = match.groups()
            seconds = float(speed) if unit == "s/it" else 1.0 / max(float(speed), 1e-6)
            found.append((int(step), int(total), seconds))
    return found


def _image(entry: CatalogEntry) -> ImageSettings:
    if entry.image is None or IMAGE_ROLE not in entry.roles:
        raise ValueError(f"{entry.id} isn't an image model")
    return entry.image


# --- running it --------------------------------------------------------------------------


class Images:
    """Pictures on this computer: the sd.cpp build, the runtime's room, the model's files, and
    what earlier pictures took."""

    def __init__(
        self,
        *,
        runtime: ModelRuntime,
        downloads: Any,  # lectern.downloads.manager.DownloadManager
        installer: Any,  # lectern.runners.sdcpp.SdCppInstaller
        variant: Callable[[], str],  # the sd.cpp build for this machine (asked once, lazily)
        sessions: sessionmaker[Session],  # the app database, for the timings
        run_dir: Path,
        clock: Callable[[], float] = time.monotonic,
        timeout: float = TIMEOUT,
    ) -> None:
        self.runtime = runtime
        self.downloads = downloads
        self.installer = installer
        self._variant_of = variant
        self._variant: str | None = None
        self.sessions = sessions
        self.run_dir = run_dir
        self.clock = clock
        self.timeout = timeout

    @property
    def tier(self) -> Tier:
        return self.runtime.tier

    def variant(self) -> str:
        if self._variant is None:
            self._variant = self._variant_of()
        return self._variant

    def engine_missing_bytes(self) -> int:
        """What the sd.cpp build still has to download (0 once installed)."""
        return self.installer.size(self.variant())

    def size_for(
        self, entry: CatalogEntry, width: int | None, height: int | None
    ) -> tuple[int, int]:
        return clamp_size(size_settings(entry, self.tier), width, height)

    # --- the expected wait ---------------------------------------------------------------

    def estimate(self, entry: CatalogEntry, width: int, height: int) -> float:
        """Seconds the picture should take: from this computer's last picture of this model at
        the nearest size, its steps and decoding scaled by the pixels; before there is one,
        the tier's first guess scaled the same way."""
        image = _image(entry)
        pixels = width * height
        with self.sessions() as session:
            rows = session.scalars(
                select(ImageTimingRow).where(ImageTimingRow.model_id == entry.id)
            ).all()
        if rows:
            row = min(rows, key=lambda r: abs(r.width * r.height - pixels))
            scale = pixels / (row.width * row.height)
            return row.load_seconds + (row.step_seconds * image.steps + row.decode_seconds) * scale
        default = size_settings(entry, self.tier).default
        guess = FIRST_GUESS_SECONDS[self.tier] * image.steps / GUESS_STEPS
        return guess * pixels / (default * default)

    def _remember(self, entry: CatalogEntry, width: int, height: int, timing: Timing) -> None:
        with self.sessions.begin() as session:
            row = session.get(ImageTimingRow, (entry.id, width, height))
            if row is None:
                row = ImageTimingRow(model_id=entry.id, width=width, height=height)
                session.add(row)
            row.load_seconds = timing.load
            row.step_seconds = timing.step
            row.decode_seconds = timing.decode

    # --- making one ----------------------------------------------------------------------

    async def make(
        self,
        entry: CatalogEntry,
        prompt: str,
        width: int,
        height: int,
        seed: int,
        on_progress: Callable[[dict], None] = lambda _event: None,
        *,
        priority: Priority = Priority.CHAT,
        since: float | None = None,
    ) -> Made:
        """Make one picture. Raises ImageFailed with a reason the chat can show; cancelled
        (Stop), sd-cli is killed and the room given back before the cancellation goes on."""
        image = _image(entry)
        files = entry.files
        offload = offloads(self.tier)
        need = need_bytes(entry, width, height, offload=offload)
        on_progress({"phase": "engine"})
        cli = await self.installer.install(self.variant())
        work = self.run_dir / "image" / uuid.uuid4().hex
        work.mkdir(parents=True)
        output = work / "picture.png"
        args = build_args(
            model=self.downloads.file_path(entry, files.model[0]),
            text_encoder=self.downloads.file_path(entry, files.text_encoder),
            vae=self.downloads.file_path(entry, files.vae),
            image=image,
            prompt=prompt,
            width=width,
            height=height,
            seed=seed,
            output=output,
            max_vram_bytes=need,
            offload=offload,
        )
        try:
            on_progress({"phase": "waiting"})
            async with self.runtime.reserve(
                IMAGE_ROLE, need, priority, since=since, model_id=entry.id
            ):
                timing = await self._run(cli, args, work, image.steps, on_progress)
            on_progress({"phase": "checking"})
            png = _checked_png(output, width, height)
        finally:
            shutil.rmtree(work, ignore_errors=True)
        self._remember(entry, width, height, timing)
        return Made(png, width, height, seed, timing)

    async def _run(
        self,
        cli: Path,
        args: list[str],
        work: Path,
        steps: int,
        on_progress: Callable[[dict], None],
    ) -> Timing:
        on_progress({"phase": "loading"})
        started = self.clock()
        process = await asyncio.create_subprocess_exec(
            str(cli),
            *args,
            cwd=work,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        tail = ""
        first_step_at: float | None = None
        last_step_at: float | None = None
        step_seconds = 0.0
        try:
            async with asyncio.timeout(self.timeout):
                stdout = process.stdout  # a pipe: asked for above
                while stdout is not None and (chunk := await stdout.read(4096)):
                    text = _ANSI.sub("", chunk.decode("utf-8", "replace"))
                    tail = (tail + text)[-LOG_TAIL:]
                    for step, total, seconds in parse_progress(text):
                        if total != steps or last_step_at is not None:
                            continue  # a VAE tile's progress, after the steps
                        now = self.clock()
                        step_seconds = seconds
                        if first_step_at is None:
                            first_step_at = now - seconds
                        on_progress({"phase": "steps", "step": step, "total": total})
                        if step >= total:
                            last_step_at = now
                            on_progress({"phase": "decoding"})
                code = await process.wait()
        except TimeoutError:
            await _kill(process)
            raise ImageFailed(
                f"Making the picture took over {self.timeout / 60:.0f} min."
            ) from None
        except BaseException:  # Stop (cancelled), or anything else: sd-cli must not stay
            await _kill(process)
            raise
        if code != 0:
            log.warning("sd-cli exited with %s: %s", code, tail[-500:])
            reason = _last_error_line(tail) or f"it exited with code {code}"
            raise ImageFailed(f"Making the picture failed: {reason}")
        ended = self.clock()
        first = first_step_at if first_step_at is not None else started
        last = last_step_at if last_step_at is not None else ended
        return Timing(
            load=max(0.0, first - started), step=step_seconds, decode=max(0.0, ended - last)
        )


async def _kill(process: asyncio.subprocess.Process) -> None:
    """End sd-cli now, and wait for it to be gone: its memory is the room being given back.
    Shielded, so a second cancellation can't leave it running."""
    if process.returncode is None:
        process.kill()
        await asyncio.shield(process.wait())


def _last_error_line(tail: str) -> str | None:
    """sd-cli's last error line ("[ERROR] …"), without its log tag."""
    for line in reversed(re.split(r"[\r\n]", tail)):
        if "[ERROR]" in line:
            return line.split("]", 1)[-1].split(" - ", 1)[-1].strip()[:200] or None
    return None


def _checked_png(path: Path, width: int, height: int) -> bytes:
    """The picture as a PNG of the size asked, written again: what sd-cli wrote is only trusted
    once it has been read as an image, and its embedded metadata is left behind."""
    if not path.is_file() or path.stat().st_size == 0:
        raise ImageFailed("Making the picture failed: no picture was written.")
    try:
        with Image.open(path) as picture:
            picture.verify()
        with Image.open(path) as picture:
            if picture.format != "PNG" or picture.size != (width, height):
                raise ImageFailed("Making the picture failed: what was written isn't the picture.")
            clean = picture.convert("RGB")
    except (OSError, SyntaxError, ValueError) as exc:  # Pillow's ways of saying "not an image"
        raise ImageFailed("Making the picture failed: what was written isn't a picture.") from exc
    out = io.BytesIO()
    clean.save(out, format="PNG", optimize=False)
    return out.getvalue()
