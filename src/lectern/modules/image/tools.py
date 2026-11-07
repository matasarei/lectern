"""The chat's image tool: `image.generate` makes a picture from a prompt the model writes, and
saves it to the project's images/ folder.

It asks first, always: on a 4 GB card a picture takes minutes and unloads the chat's model, so
a call the person didn't mean costs more than a click. The card shows the prompt, which they
can change, the shape (with each one's size and expected wait), and what has to download first.
While it runs, the chat shows its progress and a Stop that ends only the picture.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from typing import Literal

from pydantic import BaseModel, Field

from lectern.agent import model_download
from lectern.agent.tools import AgentTool, ToolContext, ToolError, ToolResult
from lectern.catalog.catalog import IMAGE_ROLE, CatalogEntry
from lectern.modules.image.generate import ImageFailed, Images, size_settings
from lectern.projects import files
from lectern.runners.binaries import UnsupportedPlatform
from lectern.runtime.supervisor import NotAvailable
from lectern.runtime.wiring import role_model

FOLDER = "images"
Shape = Literal["square", "portrait", "landscape", "large"]
SHAPES: tuple[Shape, ...] = ("square", "portrait", "landscape", "large")
SEED_LIMIT = 2**31


class GenerateParams(BaseModel):
    prompt: str = Field(
        min_length=3,
        max_length=1500,
        description="What the picture shows, written as a detailed description in English: "
        "the subject, the setting, the style, the light and the colours.",
    )
    shape: Shape = Field(
        "square",
        description="square, portrait (taller), landscape (wider), or large (a bigger square, "
        "slower).",
    )
    name: str | None = Field(
        None, max_length=80, description="A short file name for it, without folder or extension."
    )
    seed: int | None = Field(None, ge=0, lt=SEED_LIMIT, description="Only to repeat a picture.")


def shape_size(images: Images, entry: CatalogEntry, shape: Shape) -> tuple[int, int]:
    """The picture's size for a shape on this computer: the tier's default square, 3:4 and 4:3
    with as many pixels, or the largest square the tier allows."""
    size = size_settings(entry, images.tier)
    side = size.default
    if shape == "portrait":
        return images.size_for(entry, round(side * (3 / 4) ** 0.5), round(side * (4 / 3) ** 0.5))
    if shape == "landscape":
        return images.size_for(entry, round(side * (4 / 3) ** 0.5), round(side * (3 / 4) ** 0.5))
    if shape == "large":
        return images.size_for(entry, int(size.max_pixels**0.5), int(size.max_pixels**0.5))
    return images.size_for(entry, side, side)


def wait_text(seconds: float) -> str:
    """ "about 40 s", "about 3 min": never more precise than an estimate is."""
    if seconds < 60:
        return f"about {max(5, round(seconds / 5) * 5)} s"
    return f"about {round(seconds / 60)} min"


def _images(ctx: ToolContext) -> Images:
    if ctx.images is None or ctx.runtime is None:
        raise ToolError("Image generation isn't available here.")
    return ctx.images


def _model(ctx: ToolContext) -> CatalogEntry:
    entry = role_model(ctx.runtime, IMAGE_ROLE)
    if entry is None:
        raise ToolError("No image model is set for this computer.")
    return entry


def _engine_bytes(images: Images) -> int:
    try:
        return images.engine_missing_bytes()
    except UnsupportedPlatform as exc:
        raise ToolError(f"Image generation can't run here: {exc}") from None


def _question(ctx: ToolContext, params: GenerateParams) -> str:
    images = _images(ctx)
    entry = _model(ctx)
    engine = _engine_bytes(images)
    width, height = shape_size(images, entry, params.shape)
    wait = wait_text(images.estimate(entry, width, height))
    text = f"Make a {width}×{height} picture with {entry.title}? It should take {wait}."
    if not model_download.on_disk(ctx, entry):
        if (why := model_download.why_not(ctx, entry)) is not None:
            raise ToolError(f"{entry.title} isn't downloaded, and {why}.")
        text += f" First {entry.title} downloads ({entry.size / 1e9:.1f} GB)."
    if engine:
        text += f" The image engine downloads too ({engine / 1e6:.0f} MB)."
    return text


def _forced_question(ctx: ToolContext, params: GenerateParams) -> str | None:
    """Asked for with the Image toggle: no card, but for a download the person can't have
    known about."""
    images = _images(ctx)
    entry = _model(ctx)
    engine = _engine_bytes(images)
    missing = []
    if not model_download.on_disk(ctx, entry):
        if (why := model_download.why_not(ctx, entry)) is not None:
            raise ToolError(f"{entry.title} isn't downloaded, and {why}.")
        missing.append(f"{entry.title} ({entry.size / 1e9:.1f} GB)")
    if engine:
        missing.append(f"the image engine ({engine / 1e6:.0f} MB)")
    if not missing:
        return None
    return f"Making pictures needs {' and '.join(missing)} downloaded first. Download now?"


def _fields(ctx: ToolContext, params: GenerateParams) -> list[dict]:
    images = _images(ctx)
    entry = _model(ctx)

    def choice(shape: Shape) -> dict:
        width, height = shape_size(images, entry, shape)
        seconds = images.estimate(entry, width, height)
        return {"value": shape, "width": width, "height": height, "seconds": round(seconds)}

    return [
        {"name": "prompt", "kind": "text", "value": params.prompt},
        {
            "name": "shape",
            "kind": "choice",
            "value": params.shape,
            "choices": [choice(shape) for shape in SHAPES],
        },
    ]


PHASE_LABELS = {
    "engine": "Getting the image engine ready",
    "waiting": "Waiting for room on the GPU",
    "loading": "Loading the image model",
    "decoding": "Finishing the picture",
    "checking": "Checking the picture",
}


def _progress(ctx: ToolContext) -> Callable[[dict], None]:
    def report(event: dict) -> None:
        phase = event["phase"]
        if phase == "steps":
            ctx.emit(
                {
                    "type": "progress",
                    "label": "Making the picture",
                    "done": event["step"],
                    "total": event["total"],
                    "stoppable": True,
                }
            )
        else:
            ctx.emit({"type": "progress", "label": PHASE_LABELS[phase], "stoppable": True})

    return report


def _file_name(params: GenerateParams) -> str:
    """The picture's file name: the one asked for, else the prompt's first words."""
    base = params.name or params.prompt
    keep = "".join(c if c.isalnum() or c in " -_" else " " for c in base).split()
    words = keep if params.name else keep[:6]
    stem = "-".join(words).lower()[:60].strip("-") or "picture"
    return f"{stem}.png"


async def _generate(ctx: ToolContext, params: GenerateParams) -> ToolResult:
    images = _images(ctx)
    entry = _model(ctx)
    if not model_download.on_disk(ctx, entry):
        state = await model_download.download(ctx, entry)
        if state.status != "done":
            raise ToolError(f"{entry.title} didn't download: {state.error or state.status}.")
    width, height = shape_size(images, entry, params.shape)
    seed = params.seed if params.seed is not None else secrets.randbelow(SEED_LIMIT)
    try:
        made = await images.make(entry, params.prompt, width, height, seed, _progress(ctx))
    except (ImageFailed, NotAvailable) as exc:
        raise ToolError(str(exc)) from None
    except UnsupportedPlatform as exc:
        raise ToolError(f"Image generation can't run here: {exc}") from None
    with ctx.sessions.begin() as session:
        row = files.write_bytes(
            ctx.project.root, session, owner_id=ctx.user.id, name=_file_name(params),
            data=made.png, origin=files.Origin("session", id=ctx.session_id,
                                               note=f"seed {seed}: {params.prompt}"[:500]),
            folder=FOLDER,
        )  # fmt: skip
        path, artifact_id = row.path, row.id
    tab = {"kind": "artifact", "id": artifact_id}
    return ToolResult(
        f"Made {path} ({width}×{height}, seed {seed}) with {entry.title}. It is open in a tab; "
        "tell the person in a sentence, without describing the picture: you can't see it.",
        refs=[{**tab, "name": path, "ref": None}],
        action={"type": "open_tab", "tab": tab},
    )


GENERATE = AgentTool(
    name="image.generate",
    description=(
        "Make a picture from a description, on this computer, and save it to the project's "
        "images/ folder. Only when the person asks for an image, picture, drawing, photo or "
        "illustration to be made: not to describe, find or talk about one. The person "
        "confirms first and can change the prompt."
    ),
    params=GenerateParams,
    run=_generate,
    confirm=True,
    confirm_text=_question,
    confirm_fields=_fields,
    forced_question=_forced_question,
)
