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
from lectern.runners.llamacpp import InstallingLlamaCppRunner
from lectern.runtime import planner
from lectern.runtime.supervisor import ModelChoice, ModelRuntime, NotAvailable

GIB = 1024**3
CPU_RESERVE = 4 * GIB  # RAM kept for the OS and the app when models run on the CPU


def budget_for(hw: HardwareInfo) -> tuple[int, str]:
    """Bytes models may use, and where the number came from."""
    forced = os.environ.get("LECTERN_BUDGET_MB")
    if forced:
        return int(forced) * 1024**2, "LECTERN_BUDGET_MB"
    if hw.gpu_budget > 0:
        return hw.gpu_budget, "gpu"
    return max(hw.ram_total - CPU_RESERVE, 2 * GIB), "ram"


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

    def resolve(role: str) -> ModelChoice:
        entry = catalog.load().defaults_for(tier).get(role)
        if entry is None:
            raise NotAvailable(f"No model is set for the {role!r} role.")
        if not downloads.is_downloaded(entry):
            raise NotAvailable(f"{entry.title} isn't downloaded yet. Download it first.")
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
        return InstallingLlamaCppRunner(installer, variant, paths.home / "run")

    return ModelRuntime(
        budget=budget, unified=unified, tier=tier, resolve=resolve, runner_for=runner_for
    )
