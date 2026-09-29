"""One verified file download: resumable through a `.part` file and checked against a sha256
before it's moved into place, so a file without `.part` is always a verified one.

Used for model files (Hugging Face) and runner builds (GitHub releases).
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from pathlib import Path

import httpx2

CHUNK = 1024 * 1024


class DownloadError(Exception):
    pass


def _hash_file(path: Path) -> hashlib._Hash:
    hasher = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(CHUNK), b""):
            hasher.update(block)
    return hasher


async def fetch_verified(
    client: httpx2.AsyncClient,
    url: str,
    final: Path,
    *,
    size: int,
    sha256: str,
    on_bytes: Callable[[int], None] = lambda n: None,
    on_verifying: Callable[[], None] = lambda: None,
    label: str | None = None,
) -> None:
    """Download `url` to `final`. `on_bytes` gets every change in bytes on disk (negative
    when a partial file is discarded)."""
    label = label or final.name
    if final.exists():
        return
    final.parent.mkdir(parents=True, exist_ok=True)
    part = final.with_name(final.name + ".part")
    start = part.stat().st_size if part.exists() else 0
    if start > size:
        part.unlink()
        on_bytes(-start)
        start = 0

    # Re-hashing gigabytes takes seconds: do it off the event loop so the API stays live.
    hasher = await asyncio.to_thread(_hash_file, part) if start else hashlib.sha256()

    # Plain bytes only: with compression, Range offsets and sizes wouldn't match the file.
    headers = {"Accept-Encoding": "identity"}
    if start:
        headers["Range"] = f"bytes={start}-"
    async with client.stream("GET", url, headers=headers) as response:
        if response.status_code == 200 and start:
            # The server ignored the Range header: start this file again.
            on_bytes(-start)
            start = 0
            hasher = hashlib.sha256()
        elif response.status_code not in (200, 206):
            raise DownloadError(f"{label}: HTTP {response.status_code}")
        mode = "ab" if start else "wb"
        with part.open(mode) as out:
            async for chunk in response.aiter_bytes():  # as received: a drop loses nothing
                out.write(chunk)
                hasher.update(chunk)
                on_bytes(len(chunk))

    on_verifying()
    if part.stat().st_size != size:
        raise DownloadError(
            f"{label}: got {part.stat().st_size} bytes, expected {size}. Try again to resume."
        )
    if hasher.hexdigest() != sha256:
        part.unlink()
        on_bytes(-size)
        raise DownloadError(f"{label}: checksum mismatch; the partial file was removed.")
    part.replace(final)
