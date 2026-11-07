from __future__ import annotations

import pytest

from lectern.projects.preview import preview_kind
from lectern.projects.text import mime_for


@pytest.mark.parametrize(
    ("name", "kind"),
    [
        ("notes.md", "markdown"),
        ("README.MARKDOWN", "markdown"),
        ("notes.txt", "text"),
        ("data.json", "text"),
        ("feed.xml", "text"),
        ("config.yaml", "text"),
        ("script.py", "text"),
        ("server.log", "text"),
        ("table.csv", "text"),
        ("page.html", "html"),
        ("PAGE.HTM", "html"),
        ("scan.pdf", "pdf"),
        ("contract.docx", "document"),
        ("contract.odt", "document"),
        ("old.doc", "legacy"),
        ("letter.rtf", "legacy"),
        ("photo.jpg", "image"),
        ("diagram.png", "image"),
        ("logo.svg", None),
        ("archive.zip", None),
        ("no-extension", None),
    ],
)
def test_each_file_has_the_preview_its_type_gets(name, kind):
    assert preview_kind(name, mime_for(name)) == kind


def test_html_is_never_a_plain_text_preview():
    # .html is in TEXT_EXTENSIONS and its type is text/html: still its own kind, so the page
    # offers the sandboxed tab and never treats it as ordinary text.
    assert preview_kind("page.html", "text/html") == "html"
