"""Putting the files an HTML page links to inside it, so it opens in a tab of its own with no
way to fetch them (`lectern.projects.html_page`)."""

from __future__ import annotations

import base64

import pytest

from lectern.projects import html_page
from lectern.projects.html_page import inline_page

PNG = b"\x89PNG\r\n\x1a\nfake"
FILES = {
    "style.css": b"h1{color:red}",
    "app.js": b"document.body.append('</script>');",
    "logo.png": PNG,
    "icon.svg": b"<svg xmlns='http://www.w3.org/2000/svg'/>",
    "big.png": PNG + b"2",
    "css/site.css": b"body{background:url('../img/bg.png')} @import url(other.css);",
    "img/bg.png": PNG + b"bg",
    "font.woff2": b"wOF2",
    "my style.css": b"p{margin:0}",
    "notes.md": b"# secret",
    "data.json": b'{"secret": 1}',
    "doc.pdf": b"%PDF",
}


def data(mime: str, content: bytes) -> str:
    return f"data:{mime};base64,{base64.b64encode(content).decode()}"


class Reader:
    """`read` as the route gives it: a file by its path from the page's folder, or None."""

    def __init__(self, files: dict[str, bytes] = FILES) -> None:
        self.files = files
        self.asked: list[str] = []

    def __call__(self, path: str) -> bytes | None:
        self.asked.append(path)
        return self.files.get(path)


def inline(page: str, reader: Reader | None = None) -> str | None:
    out = inline_page(page.encode(), reader or Reader())
    return None if out is None else out.decode("utf-8")


def test_a_stylesheet_becomes_a_style_block_and_the_rest_stays_as_written():
    page = (
        "<!DOCTYPE html>\n<HTML><head>\n  <link rel=stylesheet href='style.css' media=screen>"
        "\n</head><body class=x>  <h1 >Hi</h1>\n</body></HTML>"
    )
    assert inline(page) == (
        '<!DOCTYPE html>\n<HTML><head>\n  <style media="screen">h1{color:red}</style>'
        "\n</head><body class=x>  <h1 >Hi</h1>\n</body></HTML>"
    )


def test_a_script_keeps_its_tag_and_timing_with_its_file_as_a_data_url():
    page = '<head><script src="app.js" defer></script></head><body><script>x()</script>'
    assert inline(page) == (
        f'<head><script src="{data("text/javascript", FILES["app.js"])}" defer></script>'
        "</head><body><script>x()</script>"
    )


def test_a_module_script_keeps_its_type():
    page = '<script type="module" src="app.js"></script><p>after</p>'
    out = inline(page)
    assert out.startswith(
        f'<script type="module" src="{data("text/javascript", FILES["app.js"])}">'
    )
    assert out.endswith("</script><p>after</p>")


def test_images_icons_posters_and_srcsets_become_data_urls():
    page = (
        '<link rel="shortcut icon" href="icon.svg"><img src="logo.png" alt="Logo">'
        '<picture><source srcset="big.png 2x, logo.png 1x"></picture>'
        '<img srcset="logo.png 480w,  https://x.example/a.png 800w" sizes="50vw">'
        '<video poster="logo.png"></video>'
    )
    png, big = data("image/png", PNG), data("image/png", FILES["big.png"])
    svg = data("image/svg+xml", FILES["icon.svg"])
    assert inline(page) == (
        f'<link rel="shortcut icon" href="{svg}"><img src="{png}" alt="Logo">'
        f'<picture><source srcset="{big} 2x, {png} 1x"></picture>'
        f'<img srcset="{png} 480w, https://x.example/a.png 800w" sizes="50vw">'
        f'<video poster="{png}"></video>'
    )


def test_urls_in_css_resolve_from_where_the_css_is():
    page = (
        '<style>h1{background:url(logo.png)} @font-face{src:url("font.woff2")}</style>'
        "<div style=\"background: url( 'logo.png' )\">x</div>"
        '<link rel="stylesheet" href="css/site.css">'
    )
    png, font = data("image/png", PNG), data("font/woff2", b"wOF2")
    bg = data("image/png", FILES["img/bg.png"])
    assert inline(page) == (
        f"<style>h1{{background:url({png})}} @font-face{{src:url({font})}}</style>"
        f'<div style="background: url({png})">x</div>'
        # A stylesheet it imports isn't one of the files a page's CSS can take in.
        f"<style>body{{background:url({bg})}} @import url(other.css);</style>"
    )


def test_a_name_is_read_without_its_query_or_fragment_and_percent_decoded():
    reader = Reader()
    out = inline('<link rel="stylesheet" href="my%20style.css?v=2#x">', reader)
    assert out == "<style>p{margin:0}</style>"
    assert reader.asked == ["my style.css"]


def test_a_path_is_resolved_before_it_is_read():
    reader = Reader()
    inline('<img src="img/../logo.png"><img src="./logo.png"><img src="../outside.png">', reader)
    assert reader.asked == ["logo.png", "../outside.png"]  # each file read once


@pytest.mark.parametrize(
    "ref",
    [
        '<link rel="stylesheet" href="https://cdn.example/a.css">',
        '<script src="//cdn.example/a.js"></script>',
        '<img src="/logo.png">',
        '<img src="data:image/png;base64,AAAA">',
        '<img src="#top">',
        '<img src="">',
        '<link rel="stylesheet" href="notes.md">',
        '<script src="data.json"></script>',
        '<img src="doc.pdf">',
        '<link rel="preload" href="logo.png">',
        '<a href="logo.png">logo</a>',
        "<style>@import 'style.css';</style>",
    ],
)
def test_what_isnt_a_side_file_of_a_kind_it_takes_is_never_read(ref):
    reader = Reader()
    assert inline(f"<p>{ref}</p>", reader) is None
    assert reader.asked == []


def test_a_file_that_isnt_there_is_left_as_it_was():
    reader = Reader({})
    assert inline('<link rel="stylesheet" href="style.css"><img src="logo.png">', reader) is None
    assert reader.asked == ["style.css", "logo.png"]


def test_a_file_over_the_limit_is_left_as_it_was(monkeypatch):
    monkeypatch.setattr(html_page, "FILE_LIMIT", len(PNG))
    out = inline('<img src="big.png"><img src="logo.png">')
    assert out == f'<img src="big.png"><img src="{data("image/png", PNG)}">'


def test_files_past_the_total_are_left_as_they_were(monkeypatch):
    monkeypatch.setattr(html_page, "TOTAL_LIMIT", len(PNG) * 2)
    out = inline('<img src="logo.png"><img src="logo.png"><img src="big.png">')
    png = data("image/png", PNG)
    assert out == f'<img src="{png}"><img src="{png}"><img src="big.png">'


def test_a_stylesheet_cant_close_its_style_block():
    reader = Reader({"style.css": b"p::after{content:'</style><script>x()</script>'}"})
    out = inline('<link rel="stylesheet" href="style.css">', reader)
    assert out == "<style>p::after{content:'<\\/style><script>x()</script>'}</style>"


def test_nothing_to_inline_gives_none_so_the_raw_bytes_are_sent():
    assert inline('<!doctype html><meta charset="windows-1251"><h1>Hi</h1>') is None
    assert inline("") is None


def test_a_page_in_another_encoding_comes_out_as_utf8_saying_so():
    page = '<html><head><META charset="windows-1251"><link rel=stylesheet href=style.css>'
    page += "</head><body><h1>Привіт</h1></body></html>"
    out = inline_page(page.encode("windows-1251"), Reader())
    assert out.decode("utf-8") == (
        '<html><head><meta charset="utf-8"><style>h1{color:red}</style>'
        "</head><body><h1>Привіт</h1></body></html>"
    )


def test_a_charset_given_as_http_equiv_is_replaced_too():
    page = (
        '<meta http-equiv="Content-Type" content="text/html; charset=koi8-r">'
        "<img src=logo.png><p>Мир</p>"
    )
    out = inline_page(page.encode("koi8-r"), Reader()).decode("utf-8")
    assert out == f'<meta charset="utf-8"><img src="{data("image/png", PNG)}"><p>Мир</p>'


@pytest.mark.parametrize(
    ("raw", "text"),
    [
        ("﻿<p>Ї</p>".encode(), "<p>Ї</p>"),  # a BOM, then nothing else to go on
        ("<p>Ї</p>".encode(), "<p>Ї</p>"),  # valid UTF-8
        ("<p>café</p>".encode("windows-1252"), "<p>café</p>"),  # not UTF-8: windows-1252
    ],
)
def test_a_page_without_a_charset_is_read_as_a_browser_would(raw, text):
    out = inline_page(raw + b'<img src="logo.png">', Reader())
    assert out.decode("utf-8") == f'{text}<img src="{data("image/png", PNG)}">'
