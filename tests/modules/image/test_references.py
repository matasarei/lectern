from __future__ import annotations

import io

import pytest
from PIL import Image

from lectern.agent.tools import ToolContext, ToolError
from lectern.core.auth import LOCAL_USER, MEMBER, User
from lectern.modules.image import references
from lectern.modules.image.references import find, load
from lectern.projects import files

SOMEONE_ELSE = User(id=LOCAL_USER.id + 1, name="someone", login="someone", role=MEMBER)


@pytest.fixture
def ctx(store):
    project = store.create(LOCAL_USER, "Pictures")
    return ToolContext(project=project, sessions=store.db(project), user=LOCAL_USER, session_id=1)


def _png(width: int, height: int, mode: str = "RGB", **save) -> bytes:  # noqa: ANN003
    out = io.BytesIO()
    colour = (10, 200, 30, 0) if mode == "RGBA" else (10, 200, 30)
    Image.new(mode, (width, height), colour).save(out, format=save.pop("format", "PNG"), **save)
    return out.getvalue()


def _add(ctx: ToolContext, name: str, data: bytes, owner: User = LOCAL_USER) -> int:
    with ctx.sessions.begin() as session:
        row = files.write_bytes(
            ctx.project.root, session, owner_id=owner.id, name=name, data=data,
            origin=files.Origin("import"), folder="photos",
        )  # fmt: skip
        return row.id


def test_a_picture_is_found_by_its_path_and_ignoring_case(ctx):
    first = _add(ctx, "cat.png", _png(64, 64))
    second = _add(ctx, "Dog.jpg", _png(64, 64, format="JPEG"))

    found = find(ctx, ["photos/cat.png", "photos/dog.JPG", "photos/cat.png"])

    assert [(f.path, f.artifact_id) for f in found] == [
        ("photos/cat.png", first),
        ("photos/Dog.jpg", second),
    ]
    assert found[0].file == ctx.project.root / "photos" / "cat.png"


def test_no_paths_find_nothing(ctx):
    assert find(ctx, []) == []
    assert find(ctx, ["  "]) == []
    assert load([], 1_000_000) == []


@pytest.mark.parametrize("path", ["photos/missing.png", "photos/cat"])
def test_a_path_that_names_no_file_is_refused(ctx, path):
    _add(ctx, "cat.png", _png(64, 64))
    with pytest.raises(ToolError, match="No picture called"):
        find(ctx, [path])


def test_someone_elses_picture_isnt_found(ctx):
    _add(ctx, "theirs.png", _png(64, 64), owner=SOMEONE_ELSE)
    with pytest.raises(ToolError, match="No picture called"):
        find(ctx, ["photos/theirs.png"])


def test_a_trashed_picture_isnt_found(ctx):
    artifact_id = _add(ctx, "cat.png", _png(64, 64))
    with ctx.sessions.begin() as session:
        files.trash(ctx.project.root, session, artifact_id)
    with pytest.raises(ToolError, match="No picture called"):
        find(ctx, ["photos/cat.png"])


@pytest.mark.parametrize(
    ("name", "data"),
    [
        ("fake.png", b"not a picture at all"),  # the content decides, not the name
        ("notes.txt", b"hello"),
        ("cut.png", _png(64, 64)[:40]),  # a broken PNG
        ("page.pdf", b"%PDF-1.7\n%..."),
    ],
)
def test_a_file_that_isnt_a_picture_is_refused(ctx, name, data):
    _add(ctx, name, data)
    with pytest.raises(ToolError, match="isn't a picture"):
        load(find(ctx, [f"photos/{name}"]), 1_000_000)


def test_a_huge_picture_is_refused_before_it_is_decoded(ctx, monkeypatch):
    monkeypatch.setattr(references, "MAX_SOURCE_PIXELS", 100 * 100)
    _add(ctx, "big.png", _png(200, 200))
    with pytest.raises(ToolError, match="too large"):
        load(find(ctx, ["photos/big.png"]), 1_000_000)


def test_a_large_photo_is_scaled_into_its_share_keeping_its_shape(ctx):
    _add(ctx, "wide.jpg", _png(4000, 3000, format="JPEG"))
    _add(ctx, "small.png", _png(200, 100))

    wide, small = load(find(ctx, ["photos/wide.jpg", "photos/small.png"]), 1024 * 1024)

    assert wide.pixels <= 1024 * 1024 // 2
    assert wide.width % 16 == 0 and wide.height % 16 == 0
    assert abs(wide.width / wide.height - 4 / 3) < 0.02
    assert (small.width, small.height) == (192, 96)  # within its share: only to 16s
    for reference in (wide, small):
        picture = Image.open(io.BytesIO(reference.png))
        assert picture.format == "PNG" and picture.mode == "RGB"
        assert picture.size == (reference.width, reference.height)


def test_the_sources_metadata_is_left_behind_and_the_picture_is_turned_upright(ctx):
    exif = Image.Exif()
    exif[0x0112] = 6  # Orientation: rotated 90° clockwise
    exif[0x010E] = "secret description"
    _add(ctx, "phone.jpg", _png(160, 80, format="JPEG", exif=exif.tobytes()))

    (reference,) = load(find(ctx, ["photos/phone.jpg"]), 1_000_000)

    assert (reference.width, reference.height) == (80, 160)
    assert b"secret description" not in reference.png
    assert not Image.open(io.BytesIO(reference.png)).getexif()


def test_transparent_parts_become_white(ctx):
    _add(ctx, "logo.png", _png(64, 64, mode="RGBA"))
    (reference,) = load(find(ctx, ["photos/logo.png"]), 1_000_000)
    assert Image.open(io.BytesIO(reference.png)).getpixel((0, 0)) == (255, 255, 255)
