from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image

from lectern.core.auth import LOCAL_USER
from lectern.modules.ocr import store as ocr_store
from lectern.modules.ocr.pages import rasterise
from lectern.modules.ocr.recognise import PageText
from lectern.projects import files
from lectern.projects.tables import OcrSessionRow, SessionRow


def _import(store, project, name: str, data: bytes) -> int:  # noqa: ANN001
    with store.db(project).begin() as session:
        return files.write_bytes(
            project.root, session, owner_id=LOCAL_USER.id, name=name, data=data,
            origin=files.Origin("import"),
        ).id  # fmt: skip


def _png(tmp_path: Path) -> bytes:
    Image.new("L", (40, 60), 255).save(tmp_path / "x.png")
    return (tmp_path / "x.png").read_bytes()


def _reading(text: str) -> PageText:
    return PageText(text, [("О", -0.1)], 900, 20, 1.5, cut_off=False)


def test_a_session_gets_a_folder_named_after_its_source_and_never_a_taken_one(store, tmp_path):
    project = store.create(LOCAL_USER, "Scans")
    (project.root / "lease-scan").mkdir()  # already taken
    source = _import(store, project, "lease-scan.png", _png(tmp_path))

    row, ocr = ocr_store.create_session(
        project.root, store.db(project), owner_id=LOCAL_USER.id, artifact_id=source
    )

    assert (row.kind, row.title) == ("ocr", "lease-scan.png")
    assert ocr.folder == "lease-scan (2)" and ocr.status == "queued"
    assert (project.root / "lease-scan (2)" / "pages").is_dir()
    with store.db(project)() as session:
        assert session.get(OcrSessionRow, row.id).source_artifact_id == source


@pytest.mark.parametrize(
    ("name", "trashed", "owner", "message"),
    [
        ("notes.txt", False, None, "isn't a PDF or an image"),
        ("scan.png", True, None, "isn't in the project any more"),
        ("scan.png", False, 999, "isn't in the project any more"),
    ],
)
def test_only_a_live_pdf_or_image_of_yours_becomes_a_session(
    store, tmp_path, name, trashed, owner, message
):  # noqa: ANN001
    project = store.create(LOCAL_USER, "Scans")
    source = _import(store, project, name, _png(tmp_path))
    if trashed:
        with store.db(project).begin() as session:
            files.trash(project.root, session, source)

    with pytest.raises(ocr_store.CannotOpen, match=message):
        ocr_store.create_session(
            project.root, store.db(project), owner_id=owner or LOCAL_USER.id, artifact_id=source
        )
    with store.db(project)() as session:  # nothing half-made
        assert not session.query(SessionRow).filter_by(kind="ocr").count()


def test_pages_are_recorded_and_each_models_reading_is_kept(store, tmp_path):
    project = store.create(LOCAL_USER, "Scans")
    source = _import(store, project, "invoice.png", _png(tmp_path))
    row, ocr = ocr_store.create_session(
        project.root, store.db(project), owner_id=LOCAL_USER.id, artifact_id=source
    )
    db = store.db(project)
    images = rasterise(
        ocr_store.source_path(project.root, db, ocr), ocr_store.pages_dir(project.root, ocr)
    )
    ocr_store.add_pages(db, row.id, images)
    (page,) = ocr_store.pages(db, row.id)
    assert (page.key, page.status, page.width, page.height) == ("p001", "pending", 40, 60)
    assert ocr_store.page_text(project.root, ocr, page) is None

    ocr_store.save_reading(project.root, db, ocr, page.id, model_id="paddleocr-vl-1.6",
                           reading=_reading("Рахунок"))  # fmt: skip
    page = ocr_store.save_reading(project.root, db, ocr, page.id, model_id="qwen3-vl-8b",
                                  reading=_reading("Рахунок № 1"))  # fmt: skip

    folder = project.root / "invoice" / "pages"
    assert ocr_store.page_text(project.root, ocr, page) == "Рахунок № 1"  # the latest reading
    assert (folder / "p001.paddleocr-vl-1.6.ocr.md").read_text(encoding="utf-8") == "Рахунок\n"
    assert json.loads((folder / "p001.qwen3-vl-8b.json").read_text())["tokens"] == [["О", -0.1]]
    assert (page.status, page.model_id, page.seconds, page.looped) == (
        "done",
        "qwen3-vl-8b",
        1.5,
        False,
    )
    assert ocr_store.page_image(project.root, ocr, page) == folder / "p001.png"
    with db() as session:
        assert session.get(OcrSessionRow, row.id).pages_total == 1


def test_a_reading_that_repeats_itself_is_flagged_and_a_failure_is_recorded(store, tmp_path):
    project = store.create(LOCAL_USER, "Scans")
    source = _import(store, project, "invoice.png", _png(tmp_path))
    row, ocr = ocr_store.create_session(
        project.root, store.db(project), owner_id=LOCAL_USER.id, artifact_id=source
    )
    db = store.db(project)
    ocr_store.add_pages(db, row.id, rasterise(ocr_store.source_path(project.root, db, ocr),
                                             ocr_store.pages_dir(project.root, ocr)))  # fmt: skip
    (page,) = ocr_store.pages(db, row.id)
    text = " ".join(f"word{i}" for i in range(15))

    assert ocr_store.save_reading(project.root, db, ocr, page.id, model_id="m",
                                  reading=_reading(f"{text}\n{text}")).looped  # fmt: skip
    failed = ocr_store.page_failed(db, page.id, "The OCR model answered 500: boom")
    assert (failed.status, failed.error) == ("failed", "The OCR model answered 500: boom")


def test_a_session_whose_source_was_trashed_says_so(store, tmp_path):
    project = store.create(LOCAL_USER, "Scans")
    source = _import(store, project, "invoice.png", _png(tmp_path))
    _, ocr = ocr_store.create_session(
        project.root, store.db(project), owner_id=LOCAL_USER.id, artifact_id=source
    )
    with store.db(project).begin() as session:
        files.trash(project.root, session, source)

    with pytest.raises(ocr_store.CannotOpen, match="moved to the trash"):
        ocr_store.source_path(project.root, store.db(project), ocr)
