"""The chat's image tool: `image.generate` makes a picture from a prompt the model writes, and
from reference pictures in the project when there are some, and saves it to the project's
images/ folder.

Before anything is asked, its `prepare` rewrites the prompt the way the image model wants it
(prompt_gate.py): the person approves the prompt that will run. The pictures to work from are
the ones the call names, else those attached to the message that asked (references.py).

It asks first, always, with the Image toggle too: on a 4 GB card a picture takes minutes and
unloads the chat's model, so a call the person didn't mean costs more than a click. The card
shows the prompt, which they can change, the reference pictures, which they can take away, the
shape (with each one's size and expected wait), and what has to download first. While it runs,
the chat shows its progress and a Stop that ends only the picture.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from lectern.agent import model_download
from lectern.agent.tools import AgentTool, ToolContext, ToolError, ToolResult
from lectern.catalog.catalog import IMAGE_ROLE, CatalogEntry
from lectern.modules.image import prompt_gate, references
from lectern.modules.image.generate import ImageFailed, Images, size_settings
from lectern.modules.image.prompt_gate import Shown
from lectern.projects import files
from lectern.projects.tables import ArtifactRow, MessageRow
from lectern.runners.binaries import UnsupportedPlatform
from lectern.runtime.supervisor import NotAvailable
from lectern.runtime.wiring import role_model

FOLDER = "images"
Shape = Literal["square", "portrait", "landscape", "large"]
SHAPES: tuple[Shape, ...] = ("square", "portrait", "landscape", "large")
SEED_LIMIT = 2**31
MAX_REFERENCES = 4  # pictures to work from in one call, whatever the model takes
VISION_ROLE = "ocr.hand"  # the model that looks at the reference pictures
PICTURE_TYPES = frozenset(
    {"image/png", "image/jpeg", "image/webp", "image/gif", "image/bmp", "image/tiff"}
)


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
    references: list[str] = Field(
        default_factory=list,
        max_length=MAX_REFERENCES,
        description="Pictures in the project to make this one from, by their names as "
        "project.list shows them: a person or a thing to keep, a style, a layout. Say in the "
        "prompt what to take from each (image 1, image 2). Leave it empty to use the pictures "
        "attached to the message.",
    )


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


# --- before the card: the references and the prompt ------------------------------------------


@dataclass
class Prepared:
    """What `prepare` found, for the card: the reference pictures, and why the prompt is still
    the chat's, or why the pictures weren't looked at, when either is so."""

    found: list[references.Found] = field(default_factory=list)
    kept_because: str | None = None
    unseen_because: str | None = None


def _takes(ctx: ToolContext) -> int:
    """How many reference pictures the image model takes itself (0: they're described)."""
    image = _model(ctx).image
    return image.references if image is not None else 0


def _attached_pictures(ctx: ToolContext) -> list[str]:
    """The pictures attached to the person's last message in this chat: the reference pictures
    when the call names none (the Image toggle never does)."""
    with ctx.sessions() as session:
        message = session.scalars(
            select(MessageRow)
            .where(MessageRow.session_id == ctx.session_id, MessageRow.role == "user")
            .order_by(MessageRow.id.desc())
            .limit(1)
        ).first()
        ids = json.loads(message.attachments or "[]") if message is not None else []
        rows = [session.get(ArtifactRow, i) for i in ids]
    return [
        r.path
        for r in rows
        if r is not None
        and r.trashed_at is None
        and r.owner_id == ctx.user.id
        and r.mime in PICTURE_TYPES
    ]


async def _vision_ready(ctx: ToolContext) -> str | None:
    """None when the vision model can look at the references, else why it can't: none here,
    not downloaded and this person can't, or they said no to downloading it."""
    if ctx.runtime is None:
        return "no model can look at them here"
    vision = role_model(ctx.runtime, VISION_ROLE)
    if vision is None:
        return "no model can look at them on this computer"
    if model_download.on_disk(ctx, vision):
        return None
    if (why := model_download.why_not(ctx, vision)) is not None:
        return f"{vision.title} isn't downloaded, and {why}"
    asked = (
        f"Reading the reference pictures needs {vision.title} ({vision.size / 1e9:.1f} GB) "
        "downloaded first. Download now?"
    )
    if not await ctx.confirm(asked):
        return f"{vision.title} wasn't downloaded"
    state = await model_download.download(ctx, vision)
    if state.status != "done":
        return f"{vision.title} didn't download: {state.error or state.status}"
    return None


async def _prepare(ctx: ToolContext, params: GenerateParams) -> GenerateParams:
    """The call made ready for its card: its reference pictures found (the ones it names, else
    the message's), and its prompt rewritten for the image model by the prompt gate, looking
    at the pictures when the vision model can."""
    _images(ctx)
    entry = _model(ctx)
    takes = _takes(ctx)
    named = bool(params.references)
    paths = params.references or await run_in_threadpool(_attached_pictures, ctx)
    found = await run_in_threadpool(references.find, ctx, paths)
    limit = takes or MAX_REFERENCES
    if len(found) > limit:
        if named:
            raise ToolError(f"{entry.title} takes at most {limit} reference pictures.")
        found = found[:limit]
    prepared = Prepared(found=found)
    shown = [Shown(f.path) for f in found]
    if found:
        prepared.unseen_because = await _vision_ready(ctx)
        if prepared.unseen_because is None:
            budget = prompt_gate.VISION_SIDE**2 * len(found)
            loaded = await run_in_threadpool(references.load, found, budget)
            shown = [Shown(f.path, picture) for f, picture in zip(found, loaded, strict=True)]
        elif not takes:
            raise ToolError(
                f"{entry.title} can't be given reference pictures, and they can't be described "
                f"for it: {prepared.unseen_because}."
            )
    gated = await prompt_gate.rewrite(
        ctx.runtime,
        params.prompt,
        guide=entry.image.prompt_guide if entry.image else None,
        references=shown,
        can_take_references=takes > 0,
    )
    prepared.kept_because = gated.kept_because
    ctx.prepared = prepared
    return params.model_copy(update={"prompt": gated.prompt, "references": [f.path for f in found]})


def _prepared(ctx: ToolContext, params: GenerateParams) -> Prepared:
    """What prepare found; found again from the parameters where it didn't run (tests)."""
    if isinstance(ctx.prepared, Prepared):
        return ctx.prepared
    return Prepared(found=references.find(ctx, params.references))


def _question(ctx: ToolContext, params: GenerateParams) -> str:
    images = _images(ctx)
    entry = _model(ctx)
    engine = _engine_bytes(images)
    prepared = _prepared(ctx, params)
    # The size and the wait are each shape's, beside it: the person may change the shape.
    text = f"Make a picture with {entry.title}?"
    if not model_download.on_disk(ctx, entry):
        if (why := model_download.why_not(ctx, entry)) is not None:
            raise ToolError(f"{entry.title} isn't downloaded, and {why}.")
        text += f" First {entry.title} downloads ({entry.size / 1e9:.1f} GB)."
    if engine:
        text += f" The image engine downloads too ({engine / 1e6:.0f} MB)."
    if prepared.found and not _takes(ctx):
        text += (
            f" {entry.title} can't be given pictures to work from: they're described in the "
            "prompt, not copied."
        )
    elif prepared.found and prepared.unseen_because:
        text += (
            f" The pictures weren't looked at to write the prompt ({prepared.unseen_because}); "
            f"{entry.title} still gets them."
        )
    if prepared.kept_because:
        text += f" The prompt wasn't rewritten for the image model: {prepared.kept_because}."
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
    found = _prepared(ctx, params).found
    # The references share the picture's own pixels (references.load), when the model takes them
    with_references = bool(found) and _takes(ctx) > 0

    def choice(shape: Shape) -> dict:
        width, height = shape_size(images, entry, shape)
        extra = width * height if with_references else 0
        seconds = images.estimate(entry, width, height, reference_pixels=extra)
        return {"value": shape, "width": width, "height": height, "seconds": round(seconds)}

    fields = [{"name": "prompt", "kind": "text", "value": params.prompt}]
    if found:
        fields.append(
            {
                "name": "references",
                "kind": "images",
                "value": [f.path for f in found],
                "choices": [
                    {
                        "value": f.path,
                        "artifact_id": f.artifact_id,
                        # the card's thumbnail, through the same route as the file's preview
                        "url": f"/api/projects/{ctx.project.id}/artifacts/{f.artifact_id}/content",
                    }
                    for f in found
                ],
            }
        )
    fields.append(
        {
            "name": "shape",
            "kind": "choice",
            "value": params.shape,
            "choices": [choice(shape) for shape in SHAPES],
        }
    )
    return fields


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
    found = await run_in_threadpool(references.find, ctx, params.references)
    given = await run_in_threadpool(references.load, found, width * height) if _takes(ctx) else []
    try:
        made = await images.make(
            entry, params.prompt, width, height, seed, _progress(ctx), references=given
        )
    except (ImageFailed, NotAvailable) as exc:
        raise ToolError(str(exc)) from None
    except UnsupportedPlatform as exc:
        raise ToolError(f"Image generation can't run here: {exc}") from None
    with ctx.sessions.begin() as session:
        row = files.write_bytes(
            ctx.project.root, session, owner_id=ctx.user.id, name=_file_name(params),
            data=made.png, origin=files.Origin("session", id=ctx.session_id,
                                               note=_note(seed, found, params.prompt,
                                                          described=not given)),
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


def _note(seed: int, found: list[references.Found], prompt: str, *, described: bool) -> str:
    """The picture's note in its From column: the seed, the references (described in the
    prompt, for a model that can't be given them), the prompt."""
    how = "described from" if described else "from"
    made_from = f", {how} {', '.join(f.path for f in found)}" if found else ""
    return f"seed {seed}{made_from}: {prompt}"[:500]


GENERATE = AgentTool(
    name="image.generate",
    # Measured on the 4B (docs/acceptance/image.md): told only when to call it, it asked for
    # details in text instead for "a cover image for…" and "a title image for…".
    description=(
        "Make a picture from a description, on this computer, and save it to the project's "
        "images/ folder. Use it whenever the person asks for a picture to be made: an image, "
        "picture, drawing, photo, illustration, logo, or a cover or title image for "
        "something. Don't ask them for details first: write a full, detailed prompt yourself "
        "from what they said and call this at once; they see the prompt on a card and can "
        "change it before anything runs. Not for describing, finding or talking about a "
        "picture."
    ),
    params=GenerateParams,
    run=_generate,
    confirm=True,
    confirm_text=_question,
    confirm_fields=_fields,
    forced_question=_forced_question,
    card_when_forced=True,
    prepare=_prepare,
)
