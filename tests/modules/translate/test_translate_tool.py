from __future__ import annotations

import asyncio
import json

import pytest

from lectern.agent.tools import ToolContext, ToolError
from lectern.catalog import catalog
from lectern.core import settings
from lectern.core.auth import LOCAL_USER
from lectern.modules.translate import languages
from lectern.modules.translate import tools as translate
from lectern.modules.translate.languages import added_languages, choose
from lectern.runners.base import RunnerError

from ...agent.fake_model import FakeHandle, make_runtime

SALAMANDRA = "salamandrata-2b"


def answer(text: str, finish: str = "stop") -> bytes:
    return json.dumps(
        {"choices": [{"message": {"content": text}, "finish_reason": finish}]}
    ).encode()


class OnDisk:
    """Which catalog models are on disk, for `choose` and the tool's downloads."""

    def __init__(self, *ids: str) -> None:
        self.ids = set(ids)

    def __call__(self, entry) -> bool:  # noqa: ANN001
        return entry.id in self.ids

    def is_downloaded(self, entry) -> bool:  # noqa: ANN001
        return entry.id in self.ids


@pytest.fixture
def translator(monkeypatch):
    """A second translation model in the catalog, which doesn't translate Japanese either."""
    real = catalog.load()
    other = real.get(SALAMANDRA).model_copy(
        update={
            "id": "other-mt",
            "title": "Other MT",
            "translate": real.get(SALAMANDRA).translate.model_copy(
                update={"languages": ["en", "uk", "de", "ja"]}
            ),
        }
    )
    extended = real.model_copy(update={"models": [*real.models, other]})
    monkeypatch.setattr(catalog, "load", lambda: extended)
    return other


# --- which model ------------------------------------------------------------------------------


def test_nothing_added_is_the_chat_model():
    assert choose("en", "uk", (), OnDisk(SALAMANDRA)) == languages.Choice(None)


def test_the_targets_model_translates_into_it():
    added = (("uk", SALAMANDRA, None),)
    assert choose("de", "uk", added, OnDisk(SALAMANDRA)).entry.id == SALAMANDRA


def test_the_sources_model_translates_out_of_it():
    added = (("uk", SALAMANDRA, None),)
    assert choose("uk", "en", added, OnDisk(SALAMANDRA)).entry.id == SALAMANDRA


def test_with_both_added_the_targets_model_goes_first(translator):
    added = (("de", "other-mt", None), ("uk", SALAMANDRA, None))
    on_disk = OnDisk(SALAMANDRA, "other-mt")
    assert choose("uk", "de", added, on_disk).entry.id == "other-mt"
    assert choose("de", "uk", added, on_disk).entry.id == SALAMANDRA


def test_a_model_that_doesnt_translate_the_other_language_falls_to_the_next(translator):
    # Salamandra doesn't translate Japanese; the other side's model does.
    added = (("uk", SALAMANDRA, None), ("ja", "other-mt", None))
    on_disk = OnDisk(SALAMANDRA, "other-mt")
    assert choose("ja", "uk", added, on_disk).entry.id == "other-mt"


def test_no_model_for_the_pair_is_the_chat_model_and_says_why():
    choice = choose("ja", "uk", (("uk", SALAMANDRA, None),), OnDisk(SALAMANDRA))
    assert choice.entry is None
    assert choice.why == "SalamandraTA 2B Instruct doesn't translate Japanese"


def test_a_model_not_downloaded_yet_uses_the_one_before_it(translator):
    added = (("uk", "other-mt", SALAMANDRA),)
    assert choose("en", "uk", added, OnDisk(SALAMANDRA)).entry.id == SALAMANDRA
    assert choose("en", "uk", added, OnDisk("other-mt", SALAMANDRA)).entry.id == "other-mt"


def test_neither_model_on_disk_is_the_chat_model_and_says_why(translator):
    choice = choose("en", "uk", (("uk", "other-mt", SALAMANDRA),), OnDisk())
    assert choice.entry is None
    assert choice.why == "the Ukrainian model isn't downloaded yet"


def test_a_model_the_catalog_dropped_is_none():
    assert choose("en", "uk", (("uk", "gone-mt", None),), OnDisk("gone-mt")).entry is None


def test_the_setting_gives_the_added_languages(store):
    sessions = store.app_sessions
    assert added_languages(sessions) == ()
    settings.put(
        sessions,
        settings.LANGUAGES_KEY,
        {
            "added": {
                "uk": {"model": SALAMANDRA, "previous": None},
                "de": {"model": "other-mt", "previous": SALAMANDRA},
                "xx": "not a choice",  # a value that isn't one is left out, not a failure
            }
        },
    )
    assert added_languages(sessions) == (("de", "other-mt", SALAMANDRA), ("uk", SALAMANDRA, None))


# --- the text in parts ------------------------------------------------------------------------


def test_short_text_is_one_part():
    assert translate.chunks("One.\n\nTwo.") == ["One.\n\nTwo."]


def test_parts_are_cut_between_paragraphs():
    first, second = "a" * 60, "b" * 60
    assert translate.chunks(f"{first}\n\n{second}", limit=100) == [first, second]


def test_a_long_paragraph_is_cut_after_a_sentence():
    text = "Short one. " * 12  # 132 characters, no paragraph break
    parts = translate.chunks(text, limit=50)
    assert all(len(p) <= 50 for p in parts)
    assert all(p.endswith(".") for p in parts)
    assert " ".join(parts) == text.strip()


def test_a_paragraph_with_nowhere_to_cut_is_cut_anyway():
    assert translate.chunks("x" * 120, limit=50) == ["x" * 50, "x" * 50, "x" * 20]


def test_empty_text_has_no_parts():
    assert translate.chunks("  \n\n  ") == []


def test_the_prompt_takes_the_text_as_it_is():
    prompt = catalog.load().get(SALAMANDRA).translate.prompt
    filled = translate.fill(prompt, "uk", "en", "Ціна {source} — 5 {грн}")
    assert filled == (
        "Translate the following text from Ukrainian into English.\n"
        "Ukrainian: Ціна {source} — 5 {грн} \nEnglish:"
    )


# --- the tool ---------------------------------------------------------------------------------


@pytest.fixture
def ctx(store):
    project = store.create(LOCAL_USER, "Letters")
    return ToolContext(project=project, sessions=store.db(project), user=LOCAL_USER, session_id=1)


def run(ctx, replies, *, added=(), on_disk=(SALAMANDRA,), text="Добрий день.", **params):  # noqa: ANN001
    handle = FakeHandle(replies)
    ctx.runtime = make_runtime(handle, per_role=True)
    ctx.downloads = OnDisk(*on_disk)
    progress: list[dict] = []
    ctx.emit = progress.append
    args = translate.TranslateParams(text=text, **{"source": "uk", "target": "en", **params})
    result = asyncio.run(translate._translate(added, ctx, args))
    return result, handle, progress


def test_an_added_language_is_translated_by_its_model(ctx):
    result, handle, progress = run(
        ctx, [answer(" Good afternoon. ")], added=(("uk", SALAMANDRA, None),)
    )
    assert result.answer == "Good afternoon."
    assert result.text == (
        "Translated from Ukrainian into English by SalamandraTA 2B Instruct. "
        "The person sees the translation as your answer."
    )
    [request] = handle.requests
    assert request["temperature"] == 0
    assert request["messages"] == [
        {
            "role": "user",
            "content": "Translate the following text from Ukrainian into English.\n"
            "Ukrainian: Добрий день. \nEnglish:",
        }
    ]
    assert progress == [
        {"type": "progress", "label": "Translating with translate@salamandrata-2b-model"}
    ]


def test_a_language_not_added_is_translated_by_the_chat_model(ctx):
    result, handle, _ = run(ctx, [answer("Good afternoon.")])
    assert result.answer == "Good afternoon."
    assert "by the chat model." in result.text
    [request] = handle.requests
    assert request["messages"][0] == {"role": "system", "content": translate.CHAT_SYSTEM}
    assert request["messages"][1]["content"] == (
        "Translate the following text from Ukrainian into English.\n\nДобрий день."
    )
    assert request["chat_template_kwargs"] == {"enable_thinking": False}


def test_a_language_whose_model_isnt_on_disk_says_why_the_chat_model_translated(ctx):
    result, _, _ = run(ctx, [answer("Hi.")], added=(("uk", SALAMANDRA, None),), on_disk=())
    assert "by the chat model (the Ukrainian model isn't downloaded yet)." in result.text


def test_a_long_text_goes_in_parts_with_progress(ctx, monkeypatch):
    monkeypatch.setattr(translate, "CHUNK_CHARS", 20)
    text = "Перший абзац тут.\n\nДругий абзац тут."
    result, handle, progress = run(ctx, [answer("First."), answer("Second.")], text=text)
    assert result.answer == "First.\n\nSecond."
    assert len(handle.requests) == 2
    assert [(p.get("done"), p.get("total")) for p in progress] == [(0, 2), (1, 2)]


def test_a_model_that_cant_start_hands_the_text_to_the_chat_model(ctx, monkeypatch):
    real = translate.translate_parts

    async def failing(ctx, entry, *args):  # noqa: ANN001, ANN202
        if entry is not None:
            raise RunnerError("llama-server exited\nlog tail")
        return await real(ctx, entry, *args)

    monkeypatch.setattr(translate, "translate_parts", failing)
    result, _, _ = run(ctx, [answer("Hi.")], added=(("uk", SALAMANDRA, None),))
    assert result.answer == "Hi."
    assert "by the chat model (SalamandraTA 2B Instruct couldn't start: llama-server exited)." in (
        result.text
    )


def test_a_cut_off_translation_is_an_error_not_half_an_answer(ctx):
    with pytest.raises(ToolError, match="too long to translate in one go"):
        run(ctx, [answer("Good aft", finish="length")])


def test_a_failed_request_is_an_error(ctx):
    handle_reply = b'{"error": "boom"}'

    class Failing(FakeHandle):
        def client(self):  # noqa: ANN202
            import httpx2

            return httpx2.AsyncClient(
                base_url=self.base_url,
                transport=httpx2.MockTransport(
                    lambda r: httpx2.Response(500, content=handle_reply)
                ),
            )

    ctx.runtime = make_runtime(Failing([]), per_role=True)
    ctx.downloads = OnDisk()
    args = translate.TranslateParams(source="uk", target="en", text="Так.")
    with pytest.raises(ToolError, match="The translation failed"):
        asyncio.run(translate._translate((), ctx, args))


def test_the_same_language_both_ways_is_refused(ctx):
    with pytest.raises(ToolError, match="source and target are the same"):
        run(ctx, [], target="uk")


@pytest.mark.parametrize("code", ["UK", "ukr-x", "u", ""])
def test_a_language_must_be_a_code(code):
    with pytest.raises(ValueError):
        translate.TranslateParams(source=code, target="en", text="x")


def test_the_tool_is_offered_every_turn_with_the_languages_added():
    from lectern.modules import ToolEnv
    from lectern.modules.translate import module

    [tool] = module.tools(ToolEnv(sandbox=False, languages=(("uk", SALAMANDRA, None),)))
    assert tool.name == "translate"
    assert tool.run.args == ((("uk", SALAMANDRA, None),),)


# --- the suggestion ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "target", "suggested"),
    [
        ("uk", "en", "uk"),  # English last: the other language is the one asked about
        ("en", "de", "de"),
        ("de", "uk", "uk"),  # the target first
        ("ja", "uk", None),  # no model here translates Japanese
        ("ja", "en", None),  # nor English to and from Japanese
    ],
)
def test_the_chat_model_translating_suggests_a_language_with_a_model(
    ctx, source, target, suggested
):
    result, _, _ = run(ctx, [answer("…")], source=source, target=target)
    expected = [{"kind": "language", "code": suggested}] if suggested else []
    assert result.refs == expected


def test_no_suggestion_when_the_languages_own_model_translated(ctx):
    result, _, _ = run(ctx, [answer("Hi.")], added=(("uk", SALAMANDRA, None),))
    assert result.refs == []


def test_no_suggestion_while_an_added_languages_model_downloads(ctx):
    result, _, _ = run(ctx, [answer("Hi.")], added=(("uk", SALAMANDRA, None),), on_disk=())
    assert result.refs == []


def test_an_added_language_isnt_suggested_again_but_the_other_one_is(ctx, translator):
    # Ukrainian's model doesn't translate Japanese; the other model, not added, does both.
    result, _, _ = run(
        ctx, [answer("…")], added=(("uk", SALAMANDRA, None),), source="uk", target="ja"
    )
    assert result.refs == [{"kind": "language", "code": "ja"}]


# --- a file -----------------------------------------------------------------------------------


def add_file(ctx, name: str, text: str | bytes, folder: str = "") -> int:  # noqa: ANN001
    from lectern.projects import files

    with ctx.sessions.begin() as session:
        data = text.encode("utf-8") if isinstance(text, str) else text
        row = files.write_bytes(
            ctx.project.root, session, owner_id=ctx.user.id, name=name, data=data,
            origin=files.Origin("import"), folder=folder,
        )  # fmt: skip
        return row.id


def saved(ctx, path: str) -> tuple[str, str, int | None]:  # noqa: ANN001
    from sqlalchemy import select

    from lectern.projects.tables import ArtifactRow

    with ctx.sessions() as session:
        row = session.scalars(select(ArtifactRow).where(ArtifactRow.path == path)).one()
        text = (ctx.project.root / row.path).read_text(encoding="utf-8")
        return text, row.from_kind, row.from_id


def run_file(ctx, replies, name: str, **params):  # noqa: ANN001, ANN202
    handle = FakeHandle(replies)
    ctx.runtime = make_runtime(handle, per_role=True)
    ctx.downloads = OnDisk(SALAMANDRA)
    ctx.emit = lambda _event: None
    args = translate.TranslateParams(file=name, **{"source": "uk", "target": "en", **params})
    return asyncio.run(translate._translate((("uk", SALAMANDRA, None),), ctx, args)), handle


def test_a_file_is_translated_into_a_new_file_beside_it_and_opened(ctx):
    source = add_file(ctx, "lease.md", "# Оренда\n\nСплатити до 10 числа.", folder="letters")
    result, handle = run_file(ctx, [answer("# Lease\n\nPay by the 10th.")], "letters/lease.md")

    assert saved(ctx, "letters/lease.en.md") == (
        "# Lease\n\nPay by the 10th.",
        "artifact",
        source,
    )
    assert result.answer is None  # the chat model says it in a sentence
    assert result.text.startswith(
        "Translated letters/lease.md from Ukrainian into English by SalamandraTA 2B Instruct, "
        "and saved it as letters/lease.en.md."
    )
    [ref] = result.refs
    assert ref["kind"] == "artifact" and ref["name"] == "letters/lease.en.md"
    assert result.action == {"type": "open_tab", "tab": {"kind": "artifact", "id": ref["id"]}}
    assert (
        "Ukrainian: # Оренда\n\nСплатити до 10 числа. \nEnglish:"
        in (handle.requests[0]["messages"][0]["content"])
    )


def test_a_taken_name_gets_a_number(ctx):
    add_file(ctx, "lease.md", "Текст.")
    add_file(ctx, "lease.en.md", "An older translation.")
    result, _ = run_file(ctx, [answer("Text.")], "lease.md")
    assert saved(ctx, "lease.en.md")[0] == "An older translation."  # never replaced
    assert result.refs[0]["name"] != "lease.en.md"
    assert saved(ctx, result.refs[0]["name"])[0] == "Text."


def test_an_ocr_documents_blocks_are_translated_without_their_anchors_or_front_matter(ctx):
    add_file(
        ctx,
        "document.md",
        "---\ndocument: Lease · version 1\n---\n\n{#b1.1 p=1} 1. Сторони\n\n{#b2.1 p=2} Текст.\n",
    )
    _, handle = run_file(ctx, [answer("1. Parties\n\nText.")], "document.md")
    [request] = handle.requests
    assert "Ukrainian: 1. Сторони\n\nТекст. \nEnglish:" in request["messages"][0]["content"]


def test_a_file_too_long_to_translate_at_once_is_refused(ctx, monkeypatch):
    monkeypatch.setattr(translate, "MAX_PARTS", 2)
    monkeypatch.setattr(translate, "CHUNK_CHARS", 10)
    add_file(ctx, "long.txt", "\n\n".join(["Абзац тут."] * 3))
    with pytest.raises(ToolError, match=r"long.txt is too long to translate at once"):
        run_file(ctx, [], "long.txt")


def test_a_file_with_no_text_is_refused(ctx):
    add_file(ctx, "photo.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
    with pytest.raises(ToolError, match="photo.png has no text to translate"):
        run_file(ctx, [], "photo.png")


def test_a_file_that_isnt_there_is_refused(ctx):
    with pytest.raises(ToolError, match="No file called 'missing.md'"):
        run_file(ctx, [], "missing.md")


@pytest.mark.parametrize("given", [{}, {"text": "Так.", "file": "lease.md"}])
def test_text_or_a_file_but_not_both_nor_neither(given):
    with pytest.raises(ValueError, match="either text or file"):
        translate.TranslateParams(source="uk", target="en", **given)
