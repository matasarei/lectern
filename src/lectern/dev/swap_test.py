"""A fake OCR job for testing the runtime: phase 1 sends every page through the `ocr` role,
phase 2 every page through `main`, the way the real OCR pipeline will (docs/ocr.md, "How a
session runs"). With a budget too small for both models it must swap exactly once; with room
for both, never.

This is not the OCR pipeline. It lives in `lectern.dev` so nobody mistakes it for one.
"""

from __future__ import annotations

import base64
import struct
import zlib
from dataclasses import dataclass, field, replace

from lectern.runtime.supervisor import ModelRuntime, Priority


def tiny_png(width: int = 64, height: int = 32) -> bytes:
    """A blank white PNG, enough to exercise a vision model's image path."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    rows = b"".join(b"\x00" + b"\xff" * width * 3 for _ in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


@dataclass
class SwapTestResult:
    pages: int
    swaps: int = 0
    done: int = 0
    phase: str = "starting"
    error: str | None = None
    finished: bool = False
    log: list[str] = field(default_factory=list)


def _page_request(role: str, page: int, *, image: bool) -> dict:
    text = f"This is page {page}. Reply with the single word OK."
    content: list[dict] | str = text
    if image:
        data = base64.b64encode(tiny_png()).decode()
        content = [
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{data}"}},
            {"type": "text", "text": text},
        ]
    return {
        "messages": [{"role": "user", "content": content}],
        "max_tokens": 4,
        "temperature": 0,
        "chat_template_kwargs": {"enable_thinking": False},
    }


async def run_swap_test(
    runtime: ModelRuntime, result: SwapTestResult, *, fake_model: bool
) -> SwapTestResult:
    """Run both phases through the runtime. With `fake_model`, the `ocr` role loads the main
    model's file as a separate process (a second model without downloading one)."""
    original_resolve = runtime.resolve
    if fake_model:

        def resolve(role: str):  # noqa: ANN202
            if role == "ocr":
                main = original_resolve("main")
                return replace(main, role="ocr", model_id=f"{main.model_id} (as ocr)")
            return original_resolve(role)

        runtime.resolve = resolve
    swaps_before = runtime.swaps
    try:
        for phase in ("ocr", "main"):
            result.phase = phase
            for page in range(1, result.pages + 1):
                async with runtime.lease(phase, Priority.JOB) as lease:
                    body = _page_request(phase, page, image=phase == "ocr" and not fake_model)
                    response = await lease.client.post("/v1/chat/completions", json=body)
                    response.raise_for_status()
                result.done += 1
                result.swaps = runtime.swaps - swaps_before
                result.log.append(f"{phase} page {page}")
    except Exception as exc:  # reported to the caller, never raised into the server
        result.error = f"{type(exc).__name__}: {exc}"
    finally:
        runtime.resolve = original_resolve
        result.swaps = runtime.swaps - swaps_before
        result.finished = True
    return result
