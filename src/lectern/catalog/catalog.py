"""The model catalog: which models Lectern can download, and the default for each role and tier.

Defaults must be permissively licensed. That rule is enforced when the catalog loads, so a
catalog edit can't quietly make a restricted model the default.
"""

from __future__ import annotations

from functools import cache
from importlib import resources
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from lectern.hardware.tiers import Tier

Role = Literal["main", "ocr", "ocr.hand", "code"]


class ModelFile(BaseModel):
    path: str
    size: int = Field(gt=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

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


class ModelFiles(BaseModel):
    model: list[ModelFile] = Field(min_length=1)  # several parts for split GGUFs
    mmproj: ModelFile | None = None

    def all(self) -> list[ModelFile]:
        return [*self.model, *([self.mmproj] if self.mmproj else [])]


class CatalogEntry(BaseModel):
    id: str
    title: str
    quant: str
    roles: list[Role]
    format: Literal["gguf", "mlx"]
    repo: str
    revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    licence: str
    permissive: bool
    defaults: dict[Role, list[Tier]] = Field(default_factory=dict)
    ctx: dict[Tier, int] = Field(default_factory=dict)
    files: ModelFiles
    notes: str | None = None

    @property
    def size(self) -> int:
        return sum(f.size for f in self.files.all())

    @property
    def licence_notice(self) -> bool:
        return not self.permissive

    @model_validator(mode="after")
    def _defaults_are_permissive_and_in_role(self) -> CatalogEntry:
        if self.defaults and not self.permissive:
            raise ValueError(f"{self.id}: a restricted-licence model can't be a default")
        stray = set(self.defaults) - set(self.roles)
        if stray:
            raise ValueError(f"{self.id}: default for roles it doesn't have: {sorted(stray)}")
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

    def get(self, model_id: str) -> CatalogEntry | None:
        return next((m for m in self.models if m.id == model_id), None)

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
