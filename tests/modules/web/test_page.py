from __future__ import annotations

from lectern.modules.web.page import decode, to_text

DOCS = """<!doctype html><html><head><title>  httpx — Streaming
  responses </title><style>body { color: red }</style>
<script>var tracking = "nope";</script></head>
<body>
<div role="navigation" class="related"><ul><li><a href="/">index</a></li><li>modules</li></ul></div>
<main>
<h1>Streaming <code>Response</code> bodies</h1>
<p>Use   <code>client.stream()</code> to read
a body <a href="https://example.com/x">in chunks</a>.</p>
<pre><code>with client.stream("GET", url) as r:
    for chunk in r.iter_bytes():
        print(len(chunk))
</code></pre>
<h2>Limits</h2>
<ul><li>One</li><li>Two <b>bold</b></li><li><img src="icon.png"></li></ul>
<table><tr><th>Name</th><th>Default</th></tr><tr><td>timeout</td><td>5 s</td></tr></table>
</main>
<footer><p>© Someone, all rights reserved</p></footer>
</body></html>"""


def test_a_docs_page_keeps_its_headings_paragraphs_code_lists_and_tables():
    page = to_text(DOCS)

    assert page.title == "httpx — Streaming responses"
    assert page.text == (
        "# Streaming `Response` bodies\n"
        "\n"
        "Use `client.stream()` to read a body in chunks.\n"
        "\n"
        "```\n"
        'with client.stream("GET", url) as r:\n'
        "    for chunk in r.iter_bytes():\n"
        "        print(len(chunk))\n"
        "```\n"
        "\n"
        "## Limits\n"
        "\n"
        "- One\n"
        "- Two bold\n"
        "\n"
        "Name | Default\n"
        "timeout | 5 s"
    )


def test_scripts_styles_menus_and_footers_are_dropped():
    text = to_text(DOCS).text
    for gone in ("tracking", "color: red", "index", "modules", "rights reserved", "https://"):
        assert gone not in text


def test_a_news_page_keeps_its_story_and_drops_the_banner_and_hidden_parts():
    news = """<html><body>
    <div role="banner"><a href="/">Site</a>
      <form><input name="q"><button>Search</button></form></div>
    <article><h2>Floods close the old bridge</h2>
    <p>The river rose by two metres<br>overnight.</p>
    <span aria-hidden="true">★</span><div hidden>Subscribe!</div>
    <p>Repairs start on Monday.</p></article>
    <aside>Most read: something else</aside>
    </body></html>"""
    assert to_text(news).text == (
        "## Floods close the old bridge\n"
        "\n"
        "The river rose by two metres\n"
        "overnight.\n"
        "\n"
        "Repairs start on Monday."
    )


def test_a_dropped_element_ends_at_its_own_closing_tag_even_with_unclosed_tags_inside():
    html = "<nav><p>menu<p>more<nav>inner</nav> still menu</nav><p>Content"
    assert to_text(html).text == "Content"


def test_a_nav_left_unclosed_drops_only_what_follows_it():
    assert to_text("<p>Before</p><nav><p>menu").text == "Before"


def test_a_code_block_keeps_its_blank_lines_and_indentation():
    html = "<pre>\ndef f():\n\n    return 1\n\n</pre>"
    assert to_text(html).text == "```\ndef f():\n\n    return 1\n```"


def test_an_empty_page_is_empty():
    assert to_text("") == to_text("<html><head></head><body></body></html>")
    assert to_text("").text == ""
    assert to_text("").title == ""


def test_entities_are_decoded():
    assert to_text("<p>Tom &amp; Jerry &mdash; &#8220;cat&#8221;</p>").text == "Tom & Jerry — “cat”"


def test_the_charset_the_server_names_comes_first():
    data = "<p>Привіт</p>".encode("cp1251")
    assert decode(data, "windows-1251") == "<p>Привіт</p>"


def test_the_pages_own_meta_charset_is_used_when_the_server_names_none():
    data = '<meta charset="windows-1251"><p>Привіт</p>'.encode("cp1251")
    assert decode(data, None).endswith("<p>Привіт</p>")
    legacy = '<meta http-equiv="Content-Type" content="text/html; charset=koi8-u"><p>Київ'
    assert decode(legacy.encode("koi8-u"), None).endswith("<p>Київ")


def test_without_a_charset_utf8_is_read_and_bad_bytes_replaced():
    assert decode("<p>Київ</p>".encode(), None) == "<p>Київ</p>"
    assert decode(b"<p>caf\xe9</p>", None) == "<p>caf\ufffd</p>"
    assert decode("\ufeff<p>x</p>".encode(), None) == "<p>x</p>"  # a byte-order mark


def test_a_charset_python_doesnt_know_falls_back_to_the_next():
    assert decode("<p>Київ</p>".encode(), "x-made-up") == "<p>Київ</p>"
