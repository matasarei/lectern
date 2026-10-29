"""The values Lectern needs from a GGUF file's header, read without the rest.

gguf's own `GGUFReader` builds every field of the header as numpy views, the tokenizer's too:
for Qwen3.5-9B (248,320 tokens) that took 9 s, and the planner asks at each model's first load,
on the event loop (docs/acceptance/m4.md, Windows results). This walks the header's key/value
section and skips what isn't wanted by its lengths.

The format (versions 2 and 3, little-endian): https://github.com/ggml-org/ggml/blob/master/docs/gguf.md
"""

from __future__ import annotations

import mmap
import struct
from pathlib import Path

MAGIC = b"GGUF"
VERSIONS = (2, 3)  # 1 had 32-bit counts; big-endian files read as a huge version
STRING = 8
ARRAY = 9
# The other value types, by number, as struct formats.
SCALARS = {
    0: "<B", 1: "<b", 2: "<H", 3: "<h", 4: "<I", 5: "<i", 6: "<f", 7: "<?", 10: "<Q", 11: "<q",
    12: "<d",
}  # fmt: skip

Value = int | float | bool | str


def read_metadata(path: Path) -> dict[str, Value]:
    """The header's values by key: scalars and strings as they are; an array of numbers as
    its first value (as the planner read it from gguf's reader); other arrays left out."""
    with path.open("rb") as file, mmap.mmap(file.fileno(), 0, access=mmap.ACCESS_READ) as data:
        if data[:4] != MAGIC:
            raise ValueError(f"{path.name} isn't a GGUF file")
        version, _tensor_count, kv_count = struct.unpack_from("<IQQ", data, 4)
        if version not in VERSIONS:
            raise ValueError(f"{path.name}: GGUF version {version} isn't one Lectern reads")
        offset = 24
        values: dict[str, Value] = {}
        for _ in range(kv_count):
            key, offset = _string(data, offset)
            (kind,) = struct.unpack_from("<I", data, offset)
            value, offset = _value(data, offset + 4, kind)
            if value is not None:
                values[key] = value
        return values


def _string(data: mmap.mmap, offset: int) -> tuple[str, int]:
    (length,) = struct.unpack_from("<Q", data, offset)
    start = offset + 8
    return data[start : start + length].decode("utf-8", errors="replace"), start + length


def _value(data: mmap.mmap, offset: int, kind: int) -> tuple[Value | None, int]:
    if kind in SCALARS:
        fmt = SCALARS[kind]
        return struct.unpack_from(fmt, data, offset)[0], offset + struct.calcsize(fmt)
    if kind == STRING:
        return _string(data, offset)
    if kind == ARRAY:
        item, count = struct.unpack_from("<IQ", data, offset)
        offset += 12
        first = (
            struct.unpack_from(SCALARS[item], data, offset)[0]
            if item in SCALARS and count
            else None
        )
        return first, _skip_items(data, offset, item, count)
    raise ValueError(f"unknown GGUF value type {kind}")


def _skip_items(data: mmap.mmap, offset: int, kind: int, count: int) -> int:
    if kind in SCALARS:
        return offset + count * struct.calcsize(SCALARS[kind])
    for _ in range(count):  # strings and arrays carry their own lengths
        if kind == STRING:
            (length,) = struct.unpack_from("<Q", data, offset)
            offset += 8 + length
        elif kind == ARRAY:
            item, inner = struct.unpack_from("<IQ", data, offset)
            offset = _skip_items(data, offset + 12, item, inner)
        else:
            raise ValueError(f"unknown GGUF value type {kind}")
    return offset
