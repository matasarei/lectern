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
    assert json.loads((folder / "p001.qwen3-vl-8b.json").read_text(encoding="utf-8"))["tokens"] == [
        ["О", -0.1]
    ]
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


def _cleaned_session(store, tmp_path: Path):  # noqa: ANN001, ANN202
    """A one-page session whose page is read and cleaned, with one doubt: "1?" in b1.1."""
    from lectern.modules.ocr.clean import CleanBlock, CleanDoubt, CleanedPage

    project = store.create(LOCAL_USER, "Review")
    source = _import(store, project, "lease.png", _png(tmp_path))
    row, ocr = ocr_store.create_session(
        project.root, store.db(project), owner_id=LOCAL_USER.id, artifact_id=source
    )
    db = store.db(project)
    images = rasterise(
        ocr_store.source_path(project.root, db, ocr), ocr_store.pages_dir(project.root, ocr)
    )
    ocr_store.add_pages(db, row.id, images)
    (page,) = ocr_store.pages(db, row.id)
    ocr_store.save_reading(project.root, db, ocr, page.id, model_id="m",
                           reading=_reading("Оплата до 1? числа."))  # fmt: skip
    cleaned = CleanedPage(
        blocks=[CleanBlock(type="paragraph", text="Оплата до 1? числа.")],
        doubts=[CleanDoubt(block=0, raw="1?", candidates=["1?", "15"])],
    )
    ocr_store.save_cleanup(project.root, db, ocr, page.id, cleaned)
    return project, db, row.id


def test_a_decision_makes_a_new_version_and_reopening_another(store, tmp_path):
    project, db, sid = _cleaned_session(store, tmp_path)
    ocr, _ = ocr_store.redo_document(project.root, db, sid, owner_id=LOCAL_USER.id)
    assert ocr.document_version == 1

    ocr, unplaced = ocr_store.decide(project.root, db, sid, "d1.1", value="15", action="accept",
                                     user_id=LOCAL_USER.id)  # fmt: skip
    doc = ocr_store.load_document(project.root, ocr)
    assert (ocr.document_version, unplaced) == (2, [])
    assert [b.id for b in doc.blocks] == ["b1.1"]  # block ids stay
    assert doc.blocks[0].doubts[0].value == "15" and doc.open_doubts() == []
    md = (project.root / ocr.folder / ocr_store.DOCUMENT_MD).read_text(encoding="utf-8")
    assert "Оплата до 15 числа." in md and "open_questions: 0" in md

    ocr, _ = ocr_store.decide(project.root, db, sid, "d1.1", value="18", action="edit",
                              user_id=LOCAL_USER.id)  # fmt: skip
    assert [d.value for d in ocr_store.decisions(db, sid)] == ["18"]  # replaced, not added

    ocr = ocr_store.reopen(project.root, db, sid, "d1.1")
    doc = ocr_store.load_document(project.root, ocr)
    assert ocr.document_version == 4 and [d.id for d in doc.open_doubts()] == ["d1.1"]
    assert ocr_store.decisions(db, sid) == []


def test_deciding_a_doubt_the_document_doesnt_have_is_refused(store, tmp_path):
    project, db, sid = _cleaned_session(store, tmp_path)
    ocr_store.redo_document(project.root, db, sid, owner_id=LOCAL_USER.id)
    with pytest.raises(ocr_store.NoSuchDoubt):
        ocr_store.decide(project.root, db, sid, "d1.9", value="x", action="edit",
                         user_id=LOCAL_USER.id)  # fmt: skip
    with pytest.raises(ocr_store.NoSuchDoubt):
        ocr_store.reopen(project.root, db, sid, "d1.1")  # open already: nothing to reopen


def test_answering_a_renumbered_page_keeps_the_answer_that_moved(store, tmp_path):
    """Page 1 is cleaned again and its "1?" becomes d1.2; answering the new d1.1 must not
    take away the answer to "1?" that was given while it was d1.1."""
    from lectern.modules.ocr.clean import CleanBlock, CleanDoubt, CleanedPage

    project, db, sid = _cleaned_session(store, tmp_path)
    ocr_store.redo_document(project.root, db, sid, owner_id=LOCAL_USER.id)
    ocr_store.decide(project.root, db, sid, "d1.1", value="15", action="accept",
                     user_id=LOCAL_USER.id)  # fmt: skip
    (page,) = ocr_store.pages(db, sid)
    cleaned_again = CleanedPage(
        blocks=[CleanBlock(type="paragraph", text="вул. Хр?щатик, оплата до 1? числа.")],
        doubts=[CleanDoubt(block=0, raw="Хр?щатик"), CleanDoubt(block=0, raw="1?")],
    )
    ocr_store.save_cleanup(project.root, db, ocr_store._ocr(db, sid), page.id, cleaned_again)
    ocr, _ = ocr_store.redo_document(project.root, db, sid, owner_id=LOCAL_USER.id)

    ocr, _ = ocr_store.decide(project.root, db, sid, "d1.1", value="Хрещатик", action="edit",
                              user_id=LOCAL_USER.id)  # fmt: skip
    doc = ocr_store.load_document(project.root, ocr)
    assert {d.id: d.value for d in doc.blocks[0].doubts} == {"d1.1": "Хрещатик", "d1.2": "15"}


def test_answering_while_a_page_is_cleaned_again_keeps_that_page_in_the_document(store, tmp_path):
    project, db, sid = _cleaned_session(store, tmp_path)
    ocr_store.redo_document(project.root, db, sid, owner_id=LOCAL_USER.id)
    (page,) = ocr_store.pages(db, sid)
    ocr_store.mark_cleaning(db, page.id, "cleaning")  # "Clean this page again" is running

    ocr, _ = ocr_store.decide(project.root, db, sid, "d1.1", value="15", action="accept",
                              user_id=LOCAL_USER.id)  # fmt: skip
    doc = ocr_store.load_document(project.root, ocr)
    assert ocr.document_version == 2 and doc.pages[0].failed is None
    assert doc.blocks[0].doubts[0].value == "15"
    ocr = ocr_store.reopen(project.root, db, sid, "d1.1")
    assert ocr_store.load_document(project.root, ocr).pages[0].failed is None


def test_a_sessions_document_lock_is_shared_while_held_and_not_kept_after(tmp_path):
    import gc

    held = ocr_store.document_lock(tmp_path, 7)
    assert ocr_store.document_lock(tmp_path, 7) is held  # a second writer waits on the same
    assert ocr_store.document_lock(tmp_path, 8) is not held
    del held
    gc.collect()
    assert (str(tmp_path), 7) not in ocr_store._locks  # nobody holds it: nothing kept
