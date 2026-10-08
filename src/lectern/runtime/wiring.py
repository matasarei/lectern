"""Build the Model Runtime for this machine: budget from the hardware check, models from the
catalog defaults for the tier, files from the downloader, llama-server from the installer."""

from __future__ import annotations

import os
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


def pick(key: str, tier: Tier) -> tuple[str, catalog.CatalogEntry]:
    """The role and model a lease key asks for. A key is a role ("ocr"), which gets the tier's
    default, or a role and a model ("ocr.hand@chandra-ocr-2"), which gets that model: the OCR
    check compares models this way, and "Re-run page with…" will use it too."""
    role, _, model_id = key.partition("@")
    models = catalog.load()
    if not model_id:
        entry = models.defaults_for(tier).get(role)
        if entry is None:
            raise NotAvailable(f"No model is set for the {role!r} role.")
        return role, entry
    entry = models.get(model_id)
    if entry is None:
        raise NotAvailable(f"There's no model {model_id!r} in the catalog.")
    if role not in entry.roles:
        raise NotAvailable(f"{entry.title} can't be used for the {role!r} role.")
    return role, entry


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

    def resolve(key: str) -> ModelChoice:
        role, entry = pick(key, tier)
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
            if not settings.get(sessions, settings.RUNTIME_KEY, settings.RUNTIME_DEFAULTS)[
                "mlx_enabled"
            ]:
                raise NotAvailable(
                    f"{choice.model_id} is an MLX model and MLX is off. Turn it on in "
                    "Models & memory, or choose a GGUF model."
                )
            if not mlx.mlx_available():
                raise NotAvailable("MLX needs Apple Silicon and `pip install lectern-ai[mlx]`.")
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
