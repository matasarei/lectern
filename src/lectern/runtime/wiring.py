"""Build the Model Runtime for this machine: budget from the hardware check, models from the
catalog defaults for the tier, files from the downloader, llama-server from the installer."""

from __future__ import annotations

import os
from collections.abc import Mapping
from functools import cache
from pathlib import Path

from lectern.catalog import catalog
from lectern.config import Paths
from lectern.core import settings
from lectern.downloads.manager import DownloadManager
from lectern.hardware.probe import HardwareInfo
from lectern.hardware.tiers import Tier, effective_tier
from lectern.runners import mlx
from lectern.runners.base import Runner
from lectern.runners.binaries import LlamaCppInstaller, UnsupportedPlatform, select_variant
from lectern.runners.llamacpp import InstallingLlamaCppRunner, install_reporting
from lectern.runtime import planner
from lectern.runtime.supervisor import (
    CHAT_IDLE_RESUME,
    ModelChoice,
    ModelRuntime,
    NotAvailable,
)

GIB = 1024**3
CPU_RESERVE = 4 * GIB  # RAM kept for the OS and the app when models run on the CPU


def budget_for(hw: HardwareInfo) -> tuple[int, str]:
    """Bytes models may use, and where the number came from."""
    forced = os.environ.get("LECTERN_BUDGET_MB")
    if forced:
        if not forced.strip().isdigit() or int(forced) <= 0:
            raise ValueError(
                f"LECTERN_BUDGET_MB must be a whole number of MiB above 0, got {forced!r}."
            )
        return int(forced) * 1024**2, "LECTERN_BUDGET_MB"
    if hw.gpu_budget > 0:
        return hw.gpu_budget, "gpu"
    return max(hw.ram_total - CPU_RESERVE, 2 * GIB), "ram"


def pick(
    key: str, tier: Tier, chosen: Mapping[str, str | None] | None = None
) -> tuple[str, catalog.CatalogEntry]:
    """The role and model a lease key asks for. A key is a role ("ocr"), which gets the model
    chosen for it in Models & memory (`chosen`, role → catalog id) or else the tier's default,
    the one recommended for this computer. Or it is a role and a model
    ("ocr.hand@chandra-ocr-2"), which gets that model: the OCR check compares models this way,
    and "Re-run page with…" will use it too."""
    role, _, model_id = key.partition("@")
    models = catalog.load()
    if not model_id:
        # A choice the catalog no longer offers for the role is as good as none.
        wanted = (chosen or {}).get(role)
        entry = next((m for m in models.choices_for(role) if m.id == wanted), None)
        entry = entry or models.defaults_for(tier).get(role)
        if entry is None:
            raise NotAvailable(f"No model is set for the {role!r} role.")
        return role, entry
    entry = models.get(model_id)
    if entry is None:
        raise NotAvailable(f"There's no model {model_id!r} in the catalog.")
    if role not in entry.roles:
        raise NotAvailable(f"{entry.title} can't be used for the {role!r} role.")
    return role, entry


def role_model(runtime: ModelRuntime, role: str) -> catalog.CatalogEntry | None:
    """The model a role uses on this computer: the one chosen for it, else the recommended
    one. None for a role nothing fills."""
    try:
        return pick(role, runtime.tier, runtime.chosen_models())[1]
    except NotAvailable:
        return None


def cleanup_choices() -> list[catalog.CatalogEntry]:
    """The models that can clean up scanned pages: the chat's, in the format that can be held
    to cleanup's JSON schema. An MLX build can't be, so it isn't one."""
    return catalog.load().choices_for("main")


def cleans_pages(runtime: ModelRuntime) -> catalog.CatalogEntry:
    """The model that cleans up scanned pages on this computer."""
    return pick(runtime.cleanup_key(), runtime.tier, runtime.chosen_models())[1]


def cleanup_model(runtime: ModelRuntime) -> catalog.CatalogEntry | None:
    """The model OCR cleanup asks for by name: one chosen for it, or a small one where the
    chat runs on its MLX build. None where cleanup uses the copy the chat already runs on."""
    return None if runtime.cleanup_key() == "main" else cleans_pages(runtime)


def keep_installed_models(sessions, downloads: DownloadManager, tier: Tier) -> None:  # noqa: ANN001
    """At start. A role nobody gave a model follows the recommendation, and a new video card
    moves the recommendation to a model that isn't downloaded. So that the role goes on
    working, the model it has becomes its choice: the largest one on disk that the catalog
    lists for this tier. Models & memory still marks the recommended one.

    Only when the tier differs from the last start's. On the same computer a role without its
    recommended model on disk was put there by a person ("Use it", before downloading), or
    never used: neither is a reason to move it. And never a restricted-licence model: like a
    default, a model a role takes unasked must be permissively licensed."""
    seen = settings.get(sessions, settings.HARDWARE_KEY, settings.HARDWARE_DEFAULTS)["tier"]
    if seen == tier.value:
        return
    if seen is not None:
        _keep_what_is_installed(sessions, downloads, tier)
    settings.put(sessions, settings.HARDWARE_KEY, {"tier": tier.value})


def _keep_what_is_installed(sessions, downloads: DownloadManager, tier: Tier) -> None:  # noqa: ANN001
    models = catalog.load()
    recommended = models.defaults_for(tier)
    chosen = settings.get(sessions, settings.MODELS_KEY, settings.MODEL_DEFAULTS)
    kept: dict[str, str] = {}
    for role, choice in chosen.items():
        default = recommended.get(role)
        if choice is not None or default is None or downloads.is_downloaded(default):
            continue
        installed = [
            m
            for m in models.choices_for(role)
            if m.permissive and tier in m.ctx and downloads.is_downloaded(m)
        ]
        if installed:
            kept[role] = max(installed, key=lambda m: m.size).id
    if kept:
        settings.put(sessions, settings.MODELS_KEY, {**chosen, **kept})


@cache
def _shape(path: Path) -> planner.ModelShape:
    return planner.read_shape(path)


def build_runtime(
    paths: Paths, downloads: DownloadManager, hw: HardwareInfo, sessions
) -> ModelRuntime:  # noqa: ANN001
    tier = effective_tier(hw)
    budget, _source = budget_for(hw)
    unified = not hw.gpus or any(g.unified for g in hw.gpus)
    installer = LlamaCppInstaller(paths.runners)
    keep_installed_models(sessions, downloads, tier)

    def chosen() -> dict[str, str | None]:
        return settings.get(sessions, settings.MODELS_KEY, settings.MODEL_DEFAULTS)

    def mlx_is_on() -> bool:
        switched_on = settings.get(sessions, settings.RUNTIME_KEY, settings.RUNTIME_DEFAULTS)[
            "mlx_enabled"
        ]
        return bool(switched_on) and mlx.mlx_available()

    def mlx_build_for(key: str, entry: catalog.CatalogEntry) -> catalog.CatalogEntry | None:
        """The MLX build that runs in place of a role's model: on a Mac with MLX switched on,
        once the build is downloaded. Until then the GGUF runs, so the switch alone breaks
        nothing. A key that names a model ("main@qwen3.5-4b") gets that model, never a build."""
        if "@" in key:
            return None
        build = catalog.load().mlx_build_of(entry)
        if build is not None and mlx_is_on() and downloads.is_downloaded(build):
            return build
        return None

    def cleanup_default() -> str:
        """The model that cleans up scanned pages when none is chosen for it. The chat's own,
        which is already loaded. Where the chat runs on its MLX build that copy can't clean
        (MLX's server can't hold a model to a JSON schema), and the chat model's GGUF beside
        its build would be the largest download there is: the minimum tier's chat model is
        used, the one cleanup was tuned and timed on."""
        _, chat = pick("main", tier, chosen())
        if mlx_build_for("main", chat) is None:
            return chat.id
        return catalog.load().defaults_for(Tier.MINIMUM)["main"].id

    def cleanup_key() -> str:
        """The lease cleanup asks for: "main" when its model is the copy the chat runs on,
        otherwise that model by name, which loads beside the chat's."""
        _, chat = pick("main", tier, chosen())
        wanted = chosen().get(settings.CLEANUP)
        # A choice the catalog no longer offers for cleanup is as good as none.
        usable = wanted in {m.id for m in cleanup_choices()}
        model_id = wanted if usable else cleanup_default()
        shares_the_chats = model_id == chat.id and mlx_build_for("main", chat) is None
        return "main" if shares_the_chats else f"main@{model_id}"

    def resolve(key: str) -> ModelChoice:
        role, entry = pick(key, tier, chosen())
        entry = mlx_build_for(key, entry) or entry
        if not downloads.is_downloaded(entry):
            raise NotAvailable(
                f"{entry.title} isn't downloaded yet. Download it in Models & memory."
            )
        model_file = downloads.file_path(entry, entry.files.model[0])
        mmproj = entry.files.mmproj
        if entry.format == "mlx":
            # An MLX model is a folder; its first file sits in it.
            model_file = model_file.parent
            shape = mlx.read_shape(model_file)
        else:
            shape = _shape(model_file)
        return ModelChoice(
            role=role,
            model_id=entry.id,
            model_file=model_file,
            weights=entry.size,
            shape=shape,
            tier_ctx=entry.ctx.get(tier) or entry.ctx.get(Tier.MINIMUM) or planner.MIN_CTX,
            mmproj=downloads.file_path(entry, mmproj) if mmproj else None,
            format=entry.format,
        )

    def runner_for(choice: ModelChoice) -> Runner:
        if choice.format == "mlx":
            if not mlx.mlx_supported():
                raise NotAvailable(
                    f"{choice.model_id} is an MLX model, and MLX only runs on Apple Silicon "
                    "Macs. Choose a GGUF model."
                )
            if not mlx.mlx_available():
                raise NotAvailable(mlx.NOT_INSTALLED)
            if not mlx_is_on():
                raise NotAvailable(
                    f"{choice.model_id} is an MLX model and MLX is off. Turn it on in "
                    "Models & memory, or choose a GGUF model."
                )
            return mlx.MlxRunner(paths.home / "run", vision=choice.role in mlx.VISION_ROLES)
        try:
            variant = select_variant(hw)
        except UnsupportedPlatform as exc:
            raise NotAvailable(str(exc)) from exc
        return InstallingLlamaCppRunner(
            installer, variant, paths.home / "run",
            on_progress=lambda *progress: runtime.engine_progress(*progress),
        )  # fmt: skip

    # The only setting so far (Models & memory): a job pauses after its current item for a
    # chat, and resumes when the chat has been idle for CHAT_IDLE_RESUME seconds.
    chat_during_job = settings.get(sessions, settings.RUNTIME_KEY, settings.RUNTIME_DEFAULTS)[
        "chat_during_job"
    ]
    try:
        engine_variant: str | None = select_variant(hw)
    except UnsupportedPlatform:
        engine_variant = None

    runtime = ModelRuntime(
        budget=budget,
        unified=unified,
        tier=tier,
        resolve=resolve,
        runner_for=runner_for,
        chat_idle_resume=CHAT_IDLE_RESUME
        if chat_during_job == "pause_job_after_current_item"
        else 0,
    )
    runtime.cleanup_key = cleanup_key
    runtime.cleanup_default = cleanup_default
    runtime.chosen_models = chosen
    # runner_for reports the engine's download to it; first-run downloads it ahead of time.
    runtime.engine = (
        Engine(installer, engine_variant, runtime) if engine_variant is not None else None
    )
    return runtime


class Engine:
    """The llama.cpp build this machine runs, for first-run to download ahead of the first
    chat (which otherwise downloads it while loading the model)."""

    def __init__(self, installer: LlamaCppInstaller, variant: str, runtime: ModelRuntime):
        self.installer = installer
        self.variant = variant
        self.runtime = runtime

    def installed(self) -> bool:
        return self.installer.server_path(self.variant) is not None

    def size(self) -> int:
        return self.installer.size(self.variant)

    async def install(self) -> None:
        await install_reporting(self.installer, self.variant, self.runtime.engine_progress)
