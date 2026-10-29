from __future__ import annotations

import struct
import subprocess
import sys
from pathlib import Path

import pytest
from gguf import GGUFReader, GGUFWriter

from lectern.runtime.gguf_header import read_metadata
from lectern.runtime.planner import ModelShape, read_shape


def _write_model(path: Path, *, head_count_kv: int | list[int] = 4) -> Path:
    """A GGUF with a model's header and a tokenizer, written by gguf's own writer."""
    writer = GGUFWriter(path, "qwen35")
    writer.add_architecture()
    writer.add_block_count(32)
    writer.add_embedding_length(4096)
    writer.add_head_count(16)
    writer.add_head_count_kv(head_count_kv)
    writer.add_key_length(256)
    writer.add_value_length(256)
    writer.add_uint32("qwen35.full_attention_interval", 4)
    writer.add_float32("qwen35.rope.freq_base", 10000.0)
    writer.add_token_list([f"токен{i}" for i in range(20000)])
    writer.add_array("tokenizer.ggml.merges", [f"a{i} b{i}" for i in range(20000)])
    writer.add_string("general.name", "Тест")
    writer.write_header_to_file()
    writer.write_kv_data_to_file()
    writer.write_tensors_to_file()
    writer.close()
    return path


def test_the_shape_is_read_from_the_header(tmp_path):
    model = _write_model(tmp_path / "m.gguf")
    assert read_shape(model) == ModelShape(
        layers=32, kv_layers=8, kv_heads=4, key_length=256, value_length=256
    )


def test_the_values_are_gguf_readers_own(tmp_path):
    model = _write_model(tmp_path / "m.gguf")
    values = read_metadata(model)
    fields = GGUFReader(model).fields
    for key in ("qwen35.block_count", "qwen35.attention.head_count", "qwen35.embedding_length",
                "qwen35.full_attention_interval"):  # fmt: skip
        field = fields[key]
        assert values[key] == int(field.parts[field.data[0]][0])
    assert values["general.architecture"] == "qwen35"
    assert values["general.name"] == "Тест"
    assert values["qwen35.rope.freq_base"] == pytest.approx(10000.0)
    # The tokenizer's arrays are walked past, not kept.
    assert "tokenizer.ggml.tokens" not in values and "tokenizer.ggml.merges" not in values


def test_an_array_of_numbers_gives_its_first_as_the_planner_had_it(tmp_path):
    model = _write_model(tmp_path / "m.gguf", head_count_kv=[2, 8, 8, 8])
    assert read_metadata(model)["qwen35.attention.head_count_kv"] == 2
    assert read_shape(model).kv_heads == 2


def test_what_isnt_a_gguf_file_is_refused(tmp_path):
    other = tmp_path / "x.gguf"
    other.write_bytes(b"PK\x03\x04" + bytes(40))
    with pytest.raises(ValueError, match="isn't a GGUF file"):
        read_metadata(other)
    old = tmp_path / "v1.gguf"
    old.write_bytes(b"GGUF" + struct.pack("<III", 1, 0, 0) + bytes(16))
    with pytest.raises(ValueError, match="version 1"):
        read_metadata(old)


def test_reading_the_shape_needs_neither_gguf_nor_numpy(tmp_path):
    """gguf's reader is what took 9 s on a 248k-token vocabulary; numpy's import, on the event
    loop, what froze a Windows tray-started Lectern. A fresh interpreter: in this one the tests
    have imported both."""
    model = _write_model(tmp_path / "m.gguf")
    check = (
        "import sys; from pathlib import Path; from lectern.runtime.planner import read_shape; "
        f"read_shape(Path({str(model)!r})); print('gguf' in sys.modules, 'numpy' in sys.modules)"
    )
    result = subprocess.run(  # noqa: S603 (fixed argv)
        [sys.executable, "-c", check], capture_output=True, text=True, check=True
    )
    assert result.stdout.split() == ["False", "False"]
