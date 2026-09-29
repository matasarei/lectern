from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import select

from lectern.core.auth import LOCAL_USER
from lectern.projects import files, search
from lectern.projects.files import BadName, Origin, safe_name
from lectern.projects.tables import ArtifactRow, now

from .conftest import write_docx, write_pdf, write_scan_pdf


async def _body(data: bytes, size: int = 7):
    for i in range(0, len(data), size):
        yield data[i : i + size]


def _import(store, project, name, data):
    return asyncio.run(
        files.import_stream(
            project.root,
            store.db(project),
            owner_id=LOCAL_USER.id,
            name=name,
            body=_body(data),
            origin=Origin("import"),
        )
    )


@pytest.mark.parametrize(
    ("given", "stored"),
    [
        ("../evil.txt", "evil.txt"),
        ("a/b.txt", "b.txt"),
        ("C:\\Users\\me\\report.md", "report.md"),
        ("bad\x00name\x1f.txt", "badname.txt"),
        ("trailing.txt. ", "trailing.txt"),
    ],
)
def test_names_are_reduced_to_a_plain_file_name(given, stored):
    assert safe_name(given) == stored


@pytest.mark.parametrize("name", [".env", "..", "", "/", ".lectern", 'a"b.txt', "a?.txt"])
def test_unusable_names_are_refused(name):
    with pytest.raises(BadName):
        safe_name(name)


def test_import_writes_inside_the_project_and_records_the_file(store):
    project = store.create(LOCAL_USER, "Imports")

    row = _import(store, project, "../../../evil.txt", b"hello")

    assert row.path == "evil.txt"
    assert (project.root / "evil.txt").read_bytes() == b"hello"
    assert not (project.root.parent / "evil.txt").exists()
    assert (row.kind, row.from_kind, row.size, row.mime) == ("imported", "import", 5, "text/plain")
    assert row.sha256 == "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
    assert list((project.root / ".lectern").glob("*.part")) == []


def test_the_same_name_twice_is_never_overwritten(store):
    project = store.create(LOCAL_USER, "Twice")
    first = _import(store, project, "notes.md", b"one")
    second = _import(store, project, "NOTES.md", b"two")
    third = _import(store, project, "notes.md", b"three")
    assert [first.path, second.path, third.path] == ["notes.md", "NOTES (2).md", "notes (3).md"]
    assert (project.root / "notes.md").read_bytes() == b"one"


def test_an_upload_over_the_limit_is_refused_and_leaves_nothing(store, monkeypatch):
    monkeypatch.setattr(files, "IMPORT_LIMIT", 10)
    project = store.create(LOCAL_USER, "Big")
    with pytest.raises(files.TooLarge):
        _import(store, project, "big.bin", b"x" * 11)
    assert sorted(p.name for p in project.root.iterdir()) == [".lectern"]
    assert list((project.root / ".lectern").glob("*.part")) == []
    with store.db(project)() as session:
        assert session.scalars(select(ArtifactRow)).all() == []


def test_markdown_docx_and_pdf_are_searchable_after_import(store, tmp_path):
    project = store.create(LOCAL_USER, "Search")
    md = _import(store, project, "notes.md", "# Оплата\nДо 10 числа кожного місяця.".encode())
    docx = _import(store, project, "lease.docx", write_docx(tmp_path / "l.docx").read_bytes())
    pdf = _import(
        store,
        project,
        "terms.pdf",
        write_pdf(tmp_path / "t.pdf", ["A late fee applies after the tenth"]).read_bytes(),
    )
    scan = _import(store, project, "scan.pdf", write_scan_pdf(tmp_path / "s.pdf").read_bytes())

    with store.db(project)() as session:
        assert [h.artifact_id for h in search.search(session, "місяця")] == [md.id]
        assert [(h.artifact_id, h.ref) for h in search.search(session, "простроч")] == [
            (docx.id, "#c1")
        ]
        assert [h.artifact_id for h in search.search(session, "late fee")] == [pdf.id]
    assert (md.text_status, docx.text_status, pdf.text_status) == ("indexed",) * 3
    assert scan.text_status == "none" and "no text layer" in scan.text_note


def test_search_treats_query_syntax_as_plain_words(store):
    project = store.create(LOCAL_USER, "Syntax")
    _import(store, project, "a.txt", b"alpha beta")
    with store.db(project)() as session:
        for query in ['"alpha', "alpha*", "alpha NEAR beta", "-alpha", "alpha OR", "()", ""]:
            search.search(session, query)  # never a syntax error
        assert len(search.search(session, 'beta"')) == 1
        assert search.search(session, "*") == []


def test_trash_moves_the_file_and_hides_it_from_search(store):
    project = store.create(LOCAL_USER, "Trash")
    row = _import(store, project, "old.txt", b"forgotten words")

    with store.db(project).begin() as session:
        trashed = files.trash(project.root, session, row.id)
        assert search.search(session, "forgotten") == []
        assert files.live(session) == []
    assert not (project.root / "old.txt").exists()
    assert (project.root / trashed.trash_path).read_bytes() == b"forgotten words"
    assert trashed.trash_path.startswith(".lectern/trash/")
    with store.db(project).begin() as session, pytest.raises(LookupError):
        files.trash(project.root, session, row.id)  # already in the trash


def test_trash_older_than_30_days_is_emptied_when_the_project_opens(store, paths):
    project = store.create(LOCAL_USER, "Purge")
    old = _import(store, project, "old.txt", b"old")
    recent = _import(store, project, "recent.txt", b"recent")
    with store.db(project).begin() as session:
        files.trash(project.root, session, old.id)
        files.trash(project.root, session, recent.id)
        session.get(ArtifactRow, old.id).trashed_at = now() - timedelta(days=31)
    store.dispose()

    from lectern.projects.store import ProjectStore

    reopened = ProjectStore(paths, store.app_sessions)
    with reopened.db(project)() as session:
        left = session.scalars(select(ArtifactRow)).all()
    reopened.dispose()
    assert [r.path for r in left] == ["recent.txt"]
    assert sorted(p.name for p in (project.root / ".lectern" / "trash").iterdir()) == [
        f"{recent.id}-recent.txt"
    ]


def test_write_text_never_replaces_a_file(store):
    project = store.create(LOCAL_USER, "Write")
    _import(store, project, "summary.md", b"by hand")
    with store.db(project).begin() as session:
        row = files.write_text(
            project.root,
            session,
            owner_id=LOCAL_USER.id,
            name="summary.md",
            text="# Summary\nby the model",
            origin=Origin("session", id=1),
        )
        assert search.search(session, "model")[0].artifact_id == row.id
    assert row.path == "summary (2).md"
    assert (row.kind, row.from_kind, row.from_id) == ("generated", "session", 1)
    assert (project.root / "summary.md").read_bytes() == b"by hand"


def test_inside_refuses_paths_out_of_the_project(store):
    project = store.create(LOCAL_USER, "Inside")
    for relative in ("../x", ".lectern/project.sqlite", "/etc/passwd"):
        with pytest.raises(BadName):
            files.inside(project.root, relative)


def test_a_name_taken_between_the_check_and_the_write_is_never_overwritten(store, monkeypatch):
    project = store.create(LOCAL_USER, "Race")
    real_free_name = files.free_name

    def racing_free_name(root, name):
        chosen = real_free_name(root, name)
        if chosen == "notes.md" and not (root / "notes.md").exists():
            (root / "notes.md").write_bytes(b"written by someone else meanwhile")
        return chosen

    monkeypatch.setattr(files, "free_name", racing_free_name)
    row = _import(store, project, "notes.md", b"mine")

    assert (project.root / "notes.md").read_bytes() == b"written by someone else meanwhile"
    assert row.path == "notes (2).md"
    assert (project.root / "notes (2).md").read_bytes() == b"mine"


def test_disks_without_hard_links_still_never_overwrite(store, monkeypatch):
    project = store.create(LOCAL_USER, "NoLinks")

    def no_links(src, dst):
        raise OSError("hard links not supported")

    monkeypatch.setattr(files.os, "link", no_links)
    first = _import(store, project, "a.txt", b"one")
    second = _import(store, project, "a.txt", b"two")
    assert (first.path, second.path) == ("a.txt", "a (2).txt")
    assert (project.root / "a.txt").read_bytes() == b"one"
    assert list((project.root / ".lectern").glob("*.part")) == []


def test_files_can_go_in_a_subfolder_and_are_never_overwritten_there(store):
    project = store.create(LOCAL_USER, "Scripts")
    with store.db(project).begin() as session:
        first = files.write_text(
            project.root, session, owner_id=LOCAL_USER.id, name="penalty.py",
            text="print(1)", origin=Origin("session", id=1), folder="scripts",
        )  # fmt: skip
        second = files.write_text(
            project.root, session, owner_id=LOCAL_USER.id, name="penalty.py",
            text="print(2)", origin=Origin("session", id=1), folder="scripts",
        )  # fmt: skip
        out = files.write_bytes(
            project.root, session, owner_id=LOCAL_USER.id, name="result.csv",
            data=b"a,b\n1,2\n", origin=Origin("artifact", id=first.id), folder="scripts/out",
        )  # fmt: skip
    assert (first.path, second.path, out.path) == (
        "scripts/penalty.py", "scripts/penalty (2).py", "scripts/out/result.csv"
    )  # fmt: skip
    assert (project.root / "scripts" / "penalty.py").read_text() == "print(1)"
    assert (project.root / "scripts" / "out" / "result.csv").exists()


@pytest.mark.parametrize("folder", ["..", "../x", ".lectern", "scripts/.hidden", "a/../../b"])
def test_a_folder_cannot_leave_the_project_or_reach_its_data(store, folder):
    project = store.create(LOCAL_USER, "Folders")
    with store.db(project).begin() as session, pytest.raises(BadName):
        files.write_text(
            project.root, session, owner_id=LOCAL_USER.id, name="x.txt", text="x",
            origin=Origin("import"), folder=folder,
        )  # fmt: skip
    assert not (project.root.parent / "x").exists()


def test_a_file_in_a_subfolder_can_be_trashed(store):
    project = store.create(LOCAL_USER, "TrashSub")
    with store.db(project).begin() as session:
        row = files.write_text(
            project.root, session, owner_id=LOCAL_USER.id, name="a.py", text="x",
            origin=Origin("import"), folder="scripts",
        )  # fmt: skip
        files.trash(project.root, session, row.id)
    assert not (project.root / "scripts" / "a.py").exists()
    assert (project.root / ".lectern" / "trash" / f"{row.id}-a.py").exists()
