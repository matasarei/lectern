"""The model catalog: which models Lectern can download, and the default for each role and tier.

Defaults must be permissively licensed. That rule is enforced when the catalog loads, so a
catalog edit can't quietly make a restricted model the default.

Defaults are GGUF models, run by llama.cpp everywhere. A model can also have an MLX build: the
same model in MLX's format, which `replaces` it on an Apple Silicon Mac when MLX is switched on
and the build is downloaded (runtime/wiring.py). A build is never a default itself.

An image model (`format: sd`, the `image` role) is run by stable-diffusion.cpp, not llama.cpp.
It has three pieces, often from three repositories: the diffusion model, the text encoder and
the VAE, so each of those files may name its own `repo` and `revision`.
"""

from __future__ import annotations

import hashlib
from functools import cache
from importlib import resources
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from lectern.hardware.tiers import Tier

Role = Literal["main", "ocr", "ocr.hand", "code", "image"]
OCR_ROLES = frozenset({"ocr", "ocr.hand"})
IMAGE_ROLE = "image"


class ModelFile(BaseModel):
    path: str
    size: int = Field(gt=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    # A file from another repository than the entry's (an image model's text encoder or VAE).
    # Both or neither: a repository without its pinned revision would follow its main branch.
    repo: str | None = None
    revision: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")

    @field_validator("path")
    @classmethod
    def _stays_in_the_model_folder(cls, path: str) -> str:
        # Downloads are written to <models>/<id>/<path>; nothing may point outside that.
        parts = path.split("/")
        if (
            not path
            or path.startswith("/")
            or "\\" in path
            or any(p in ("", ".", "..") for p in parts)
        ):
            raise ValueError(f"file path must be a plain relative path: {path!r}")
        return path

    @model_validator(mode="after")
    def _repo_and_revision_together(self) -> ModelFile:
        if (self.repo is None) != (self.revision is None):
            raise ValueError(f"{self.path}: a file's own repo needs its own revision, and back")
        return self


class ModelFiles(BaseModel):
    model: list[ModelFile] = Field(min_length=1)  # several parts for split GGUFs
    mmproj: ModelFile | None = None
    text_encoder: ModelFile | None = None  # an image model's
    vae: ModelFile | None = None  # an image model's

    def all(self) -> list[ModelFile]:
        extra = (self.mmproj, self.text_encoder, self.vae)
        return [*self.model, *(f for f in extra if f is not None)]

    @model_validator(mode="after")
    def _one_place_per_file(self) -> ModelFiles:
        # They all land in the model's one folder: two with the same path would overwrite.
        paths = [f.path for f in self.all()]
        if len(paths) != len(set(paths)):
            raise ValueError(f"two files with the same path: {paths}")
        return self


class OcrSettings(BaseModel):
    """How an OCR model is asked to read a page: its own prompt, and the longest side (px)
    of the page image it gets. Page images are 300 dpi, far more than a vision model's
    context holds, so each model gets a copy scaled to what it can take."""

    prompt: str = Field(min_length=1)
    max_side: int = Field(ge=448, le=4096)


class ImageSize(BaseModel):
    """An image model's sizes on one tier: the square side it makes when none is asked for,
    and the most pixels a picture may have there (a larger ask is scaled down to it)."""

    default: int = Field(ge=256, le=2048, multiple_of=16)
    max_pixels: int = Field(ge=256 * 256, le=2048 * 2048)


class ImageSettings(BaseModel):
    """How stable-diffusion.cpp runs an image model: steps, guidance and sampler, and the
    sizes per tier. A tier missing here isn't one the model is meant for."""

    steps: int = Field(ge=1, le=100)
    cfg: float = Field(ge=0.0, le=30.0)
    sampler: str = Field(pattern=r"^[a-z0-9_+]+$")
    sizes: dict[Tier, ImageSize] = Field(min_length=1)

    @model_validator(mode="after")
    def _the_default_fits(self) -> ImageSettings:
        for tier, size in self.sizes.items():
            if size.default * size.default > size.max_pixels:
                raise ValueError(f"{tier.value}: the default size is over max_pixels")
        return self


class CatalogEntry(BaseModel):
    id: str
    title: str
    quant: str
    roles: list[Role]
    format: Literal["gguf", "mlx", "sd"]
    repo: str
    revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    licence: str
    permissive: bool
    defaults: dict[Role, list[Tier]] = Field(default_factory=dict)
    ctx: dict[Tier, int] = Field(default_factory=dict)
    files: ModelFiles
    ocr: OcrSettings | None = None  # for models with an OCR role
    image: ImageSettings | None = None  # for an image model
    replaces: str | None = None  # an MLX build: the id of the GGUF model it stands in for
    notes: str | None = None

    @property
    def size(self) -> int:
        return sum(f.size for f in self.files.all())

    @property
    def licence_notice(self) -> bool:
        return not self.permissive

    def source(self, file: ModelFile) -> str:
        """Where a file downloads from on Hugging Face: `<repo>/resolve/<revision>/<path>`,
        the file's own repo and revision when it names them, else the entry's."""
        repo, revision = (file.repo, file.revision) if file.repo else (self.repo, self.revision)
        return f"{repo}/resolve/{revision}/{file.path}"

    @property
    def pin(self) -> str:
        """The version a download is recorded at, to notice that the catalog pins a newer one:
        the entry's revision, or, when some files come from other repos, a digest of every
        revision, so that a newer pin of any piece shows as an update."""
        own = [f.revision for f in self.files.all() if f.revision]
        if not own:
            return self.revision
        return hashlib.sha256("|".join([self.revision, *own]).encode()).hexdigest()[:40]

    def suits(self, tier: Tier) -> bool:
        """Listed for that tier: a context for it, or an image size."""
        return tier in self.ctx or (self.image is not None and tier in self.image.sizes)

    @model_validator(mode="after")
    def _defaults_are_permissive_and_in_role(self) -> CatalogEntry:
        if self.defaults and not self.permissive:
            raise ValueError(f"{self.id}: a restricted-licence model can't be a default")
        stray = set(self.defaults) - set(self.roles)
        if stray:
            raise ValueError(f"{self.id}: default for roles it doesn't have: {sorted(stray)}")
        reads_pages = bool(OCR_ROLES & set(self.roles))
        if reads_pages and self.ocr is None:
            raise ValueError(f"{self.id}: an OCR model needs an `ocr:` block (prompt, max_side)")
        if self.ocr is not None and not reads_pages:
            raise ValueError(f"{self.id}: only models with an OCR role take an `ocr:` block")
        return self

    @model_validator(mode="after")
    def _an_image_model_has_its_pieces(self) -> CatalogEntry:
        makes_images = IMAGE_ROLE in self.roles
        if makes_images != (self.format == "sd"):
            raise ValueError(f"{self.id}: the image role and `format: sd` go together")
        if not makes_images:
            if self.image is not None or self.files.text_encoder or self.files.vae:
                raise ValueError(f"{self.id}: only an image model has `image:`, an encoder, a VAE")
            return self
        if self.roles != [IMAGE_ROLE]:
            raise ValueError(f"{self.id}: an image model has the image role only")
        if self.image is None:
            raise ValueError(f"{self.id}: an image model needs an `image:` block")
        if self.files.text_encoder is None or self.files.vae is None or self.files.mmproj:
            raise ValueError(f"{self.id}: an image model has a text encoder and a VAE")
        if len(self.files.model) != 1:
            raise ValueError(f"{self.id}: an image model is one diffusion-model file")
        stray = {t for ts in self.defaults.values() for t in ts} - set(self.image.sizes)
        if stray:
            raise ValueError(f"{self.id}: a default for tiers it has no size for")
        return self

    @model_validator(mode="after")
    def _an_mlx_build_stands_in_for_a_model(self) -> CatalogEntry:
        if self.format != "mlx":
            if self.replaces is not None:
                raise ValueError(f"{self.id}: only an MLX build replaces another model")
            return self
        if self.replaces is None:
            raise ValueError(f"{self.id}: an MLX build says which model it `replaces`")
        if self.defaults:
            raise ValueError(f"{self.id}: an MLX build can't be a default")
        # An MLX model is a folder, and the runtime takes it from the first file's place.
        if self.files.model[0].path != "config.json":
            raise ValueError(f"{self.id}: an MLX build's first file is its config.json")
        return self


class Catalog(BaseModel):
    schema_version: int = Field(alias="schema")
    models: list[CatalogEntry]

    @model_validator(mode="after")
    def _one_default_per_role_and_tier(self) -> Catalog:
        seen: dict[tuple[str, str], str] = {}
        ids: set[str] = set()
        for entry in self.models:
            if entry.id in ids:
                raise ValueError(f"duplicate catalog id {entry.id}")
            ids.add(entry.id)
            for role, tiers in entry.defaults.items():
                for tier in tiers:
                    key = (role, tier.value)
                    if key in seen:
                        raise ValueError(f"two defaults for {key}: {seen[key]} and {entry.id}")
                    seen[key] = entry.id
        return self

    @model_validator(mode="after")
    def _each_mlx_build_replaces_its_own_gguf_model(self) -> Catalog:
        replaced: dict[str, str] = {}
        for build in self.models:
            if build.replaces is None:
                continue
            model = self.get(build.replaces)
            if model is None:
                raise ValueError(
                    f"{build.id}: replaces {build.replaces!r}, which isn't in the catalog"
                )
            if model.format != "gguf":
                raise ValueError(f"{build.id}: what it replaces must be a GGUF model")
            if set(model.roles) != set(build.roles):
                raise ValueError(f"{build.id}: must have the same roles as {model.id}")
            if model.id in replaced:
                raise ValueError(f"two MLX builds of {model.id}: {replaced[model.id]}, {build.id}")
            replaced[model.id] = build.id
        return self

    def get(self, model_id: str) -> CatalogEntry | None:
        return next((m for m in self.models if m.id == model_id), None)

    def mlx_build_of(self, model: CatalogEntry) -> CatalogEntry | None:
        """The MLX build that stands in for this model, when it has one."""
        return next((m for m in self.models if m.replaces == model.id), None)

    def choices_for(self, role: str) -> list[CatalogEntry]:
        """The models a role can be given, in catalog order. An MLX build isn't one: it
        comes with its model."""
        return [m for m in self.models if role in m.roles and m.replaces is None]

    def defaults_for(self, tier: Tier) -> dict[str, CatalogEntry]:
        """Role → default model for this tier."""
        return {
            role: entry
            for entry in self.models
            for role, tiers in entry.defaults.items()
            if tier in tiers
        }


def parse(text: str) -> Catalog:
    return Catalog.model_validate(yaml.safe_load(text))


@cache
def load() -> Catalog:
    text = resources.files("lectern.catalog").joinpath("models.yaml").read_text(encoding="utf-8")
    return parse(text)
