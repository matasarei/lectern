"""Every text file Lectern reads or writes names its encoding.

Without one, Python uses the system's code page: cp1251 on a Russian or Ukrainian Windows.
A script with "₴" couldn't be written, Cyrillic in one was a SyntaxError in the sandbox, and a
manifest's input named in Cyrillic came back garbled (docs/acceptance/m4.md, Windows results).
The tray app sets PYTHONUTF8; `lectern serve` started otherwise doesn't have it.

Ruff can't hold this: its unspecified-encoding rule doesn't look at pathlib's read_text and
write_text, where these were.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "lectern"

# `.open(...)` on these isn't a text file opened in a code page; `files.write_text` is the
# project's own, which encodes UTF-8 (projects/files.py).
NOT_TEXT_FILES = {"os", "Image", "tarfile", "zipfile", "webbrowser"}
ENCODES_ITSELF = {"files"}


def _mode(call: ast.Call, position: int) -> object:
    """The call's mode: its text when it's written out, None when absent, ... when it can't be
    known here (a variable)."""
    node = call.args[position] if len(call.args) > position else None
    node = next((k.value for k in call.keywords if k.arg == "mode"), node)
    if node is None:
        return None
    return node.value if isinstance(node, ast.Constant) else ...


def _text(mode: object) -> bool:
    return mode is None or (isinstance(mode, str) and "b" not in mode)


def unencoded(source: str) -> list[int]:
    """The lines of `source` that read or write text without naming its encoding."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call) or any(k.arg == "encoding" for k in node.keywords):
            continue
        func = node.func
        owner = (
            func.value.id
            if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)
            else None
        )
        if isinstance(func, ast.Attribute) and func.attr in ("read_text", "write_text"):
            if owner not in ENCODES_ITSELF:
                found.append(node.lineno)
        elif isinstance(func, ast.Attribute) and func.attr == "open":
            if owner not in NOT_TEXT_FILES and _text(_mode(node, 0)):
                found.append(node.lineno)
        elif isinstance(func, ast.Name) and func.id == "open" and _text(_mode(node, 1)):
            found.append(node.lineno)
    return found


def test_every_text_file_lectern_reads_or_writes_names_its_encoding():
    offenders = [
        f"{path.relative_to(SRC.parent)}:{line}"
        for path in sorted(SRC.rglob("*.py"))
        for line in unencoded(path.read_text(encoding="utf-8"))
    ]
    assert offenders == [], 'name the encoding (encoding="utf-8"): ' + ", ".join(offenders)


@pytest.mark.parametrize(
    ("source", "lines"),
    [
        ("p.read_text()", [1]),
        ("p.write_text(s)", [1]),
        ('p.read_text(encoding="utf-8")', []),
        ('p.read_text(errors="replace")', [1]),
        ("open(name)", [1]),
        ('open(name, "w")', [1]),
        ('open(name, "rb")', []),
        ('open(name, mode="wb")', []),
        ('p.open("a")', [1]),
        ('p.open("ab")', []),
        ("p.open(mode)", []),  # can't be known here: fetch.py's part.open("ab" or "wb")
        ("os.open(path, flags, 0o600)", []),
        ("Image.open(source)", []),
        ("tarfile.open(archive)", []),
        ("webbrowser.open(url)", []),
        ("files.write_text(root, session, text=text)", []),
        ('os.fdopen(fd, "w", encoding="utf-8")', []),
    ],
)
def test_what_counts_as_text_without_an_encoding(source, lines):
    assert unencoded(source) == lines
