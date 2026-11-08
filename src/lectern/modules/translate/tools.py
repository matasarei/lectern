"""The chat's translation tool: `translate` translates text or a project file on this computer,
with the model of a language an admin added (languages.py `choose`), or else with the chat
model.

Text's translation is the turn's answer as it stands (`ToolResult.answer`): the chat model
doesn't copy it into a reply of its own, so it can't reword it, and on a small GPU it isn't
loaded again just for that. A file's is saved as a new file beside it, and opened in a tab.
"""

from __future__ import annotations

from functools import partial
from pathlib import PurePosixPath

from pydantic import BaseModel, Field, model_validator
from starlette.concurrency import run_in_threadpool

from lectern.agent import model_download
from lectern.agent.core_tools import _artifact_by_name, _file_ref
from lectern.agent.tools import AgentTool, ToolContext, ToolError, ToolResult
from lectern.catalog.catalog import CatalogEntry
from lectern.catalog.languages import name
from lectern.modules.translate.languages import Added, chat_model, choose, options
from lectern.projects import files, search
from lectern.projects.tables import ArtifactRow
from lectern.projects.text import TEXT_EXTENSIONS, _read_text
from lectern.runners.base import RunnerError
from lectern.runtime.supervisor import NotAvailable, Priority

CHUNK_CHARS = 3000  # of the source per request: with its translation, within a 4k context
MAX_TOKENS = 2048  # of one part's translation
TEXT_CHARS = 20_000
MAX_PARTS = 40  # of a file: about 120,000 characters, some fifty pages
CODE = r"^[a-z]{2,3}$"

# The chat model's own: a general model, told to translate and nothing else.
CHAT_SYSTEM = (
    "You are a translator. Reply with the translation only: no notes, no quotes around it, "
    "nothing before or after it. Keep the formatting: line breaks, Markdown, lists, numbers."
)
CHAT_PROMPT = "Translate the following text from {source} into {target}.\n\n{text}"


class TranslateParams(BaseModel):
    source: str = Field(
        pattern=CODE, description='The language the text is in, as an ISO 639-1 code: "uk".'
    )
    target: str = Field(
        pattern=CODE, description='The language to translate it into, as an ISO 639-1 code: "en".'
    )
    text: str | None = Field(
        None,
        min_length=1,
        max_length=TEXT_CHARS,
        description="The text to translate, copied exactly as the person gave it.",
    )
    file: str | None = Field(
        None,
        min_length=1,
        max_length=500,
        description="Instead of text: the name of a project file to translate. The "
        "translation is saved as a new file beside it.",
    )

    @model_validator(mode="after")
    def _text_or_a_file(self) -> TranslateParams:
        if (self.text is None) == (self.file is None):
            raise ValueError("give either text or file, not both")
        return self


def chunks(text: str, limit: int | None = None) -> list[str]:
    """The text in parts of at most `limit` characters (CHUNK_CHARS unless given), cut between
    paragraphs, else between sentences or lines, else anywhere: a part is translated in one
    request."""
    limit = limit or CHUNK_CHARS
    parts: list[str] = []
    current = ""
    for paragraph in (p for p in text.strip().split("\n\n") if p.strip()):
        for piece in _pieces(paragraph, limit):
            if current and len(current) + 2 + len(piece) > limit:
                parts.append(current)
                current = ""
            current = f"{current}\n\n{piece}" if current else piece
    if current:
        parts.append(current)
    return parts


def _pieces(paragraph: str, limit: int) -> list[str]:
    """One paragraph, cut where it has to be."""
    pieces: list[str] = []
    rest = paragraph
    while len(rest) > limit:
        window = rest[:limit]
        cut = max(window.rfind(mark) for mark in (". ", "! ", "? ", "\n"))
        end = cut + 1 if cut > limit // 2 else limit
        pieces.append(rest[:end].strip())
        rest = rest[end:].strip()
    if rest:
        pieces.append(rest)
    return pieces


def fill(prompt: str, source: str, target: str, text: str) -> str:
    """A model's prompt with the languages' names and the text. Not str.format: the text may
    have braces of its own, and is put in last so nothing in it is replaced."""
    filled = prompt.replace("{source}", name(source)).replace("{target}", name(target))
    return filled.replace("{text}", text)


async def _ask(client, messages: list[dict]) -> str:  # noqa: ANN001
    response = await client.post(
        "/v1/chat/completions",
        json={
            "messages": messages,
            "max_tokens": MAX_TOKENS,
            "temperature": 0,
            "stream": False,
            "chat_template_kwargs": {"enable_thinking": False},
        },
    )
    if response.status_code != 200:
        raise ToolError(f"The translation failed: {response.text[:300]}")
    choice = response.json()["choices"][0]
    if choice.get("finish_reason") == "length":
        raise ToolError("A part of the text was too long to translate in one go.")
    return (choice["message"].get("content") or "").strip()


def _messages(entry: CatalogEntry | None, source: str, target: str, part: str) -> list[dict]:
    if entry is None:
        return [
            {"role": "system", "content": CHAT_SYSTEM},
            {"role": "user", "content": fill(CHAT_PROMPT, source, target, part)},
        ]
    return [{"role": "user", "content": fill(entry.translate.prompt, source, target, part)}]


async def translate_parts(
    ctx: ToolContext, entry: CatalogEntry | None, source: str, target: str, parts: list[str]
) -> str:
    """Each part translated in turn, with progress in the chat, by the language's model or,
    with `entry` None, the chat model."""
    key = f"translate@{entry.id}" if entry is not None else "main"
    done: list[str] = []
    async with ctx.runtime.lease(key, Priority.CHAT) as lease:
        for number, part in enumerate(parts, start=1):
            event = {"type": "progress", "label": f"Translating with {lease.model_id}"}
            if len(parts) > 1:
                event.update(done=number - 1, total=len(parts))
            ctx.emit(event)
            done.append(await _ask(lease.client, _messages(entry, source, target, part)))
    return "\n\n".join(done)


def file_text(ctx: ToolContext, name: str) -> tuple[ArtifactRow, str]:
    """A project file and its text. A text file as it is on disk; anything else (a PDF, a DOCX,
    an OCR Document) as the project indexed it, a block to a paragraph, without the Document's
    block anchors."""
    with ctx.sessions() as session:
        row = _artifact_by_name(session, ctx, name)
        indexed = search.chunks(session, row.id)
    is_document = any(c.label for c in indexed)
    if PurePosixPath(row.path).suffix.lower() in TEXT_EXTENSIONS and not is_document:
        text = _read_text(files.inside(ctx.project.root, row.path)) or ""
    else:
        # A Document's front matter (its title and version, between --- lines) isn't text.
        kept = [c for c in indexed if not (is_document and c.label is None)]
        text = "\n\n".join(c.body.strip() for c in kept if c.body.strip())
    if not text.strip():
        raise ToolError(f"{row.path} has no text to translate ({row.text_note or 'none found'}).")
    return row, text


def _save(ctx: ToolContext, source: ArtifactRow, target: str, text: str) -> ArtifactRow:
    """The translation as a new file beside the source: lease.md → lease.en.md. Never replaces
    a file: a taken name gets a number."""
    path = PurePosixPath(source.path)
    folder = "" if str(path.parent) == "." else str(path.parent)
    with ctx.sessions.begin() as session:
        return files.write_text(
            ctx.project.root, session, owner_id=ctx.user.id, name=f"{path.stem}.{target}.md",
            text=text, origin=files.Origin("artifact", id=source.id), folder=folder,
        )  # fmt: skip


async def _translate(
    added: tuple[Added, ...], ctx: ToolContext, params: TranslateParams
) -> ToolResult:
    if params.source == params.target:
        raise ToolError("The text is already in that language: source and target are the same.")
    if ctx.runtime is None:
        raise ToolError("Translation isn't available here.")
    source_file = None
    text = params.text or ""
    if params.file is not None:
        source_file, text = await run_in_threadpool(file_text, ctx, params.file)
    parts = chunks(text)
    if not parts:
        raise ToolError("There's no text to translate.")
    if len(parts) > MAX_PARTS:
        raise ToolError(
            f"{source_file.path if source_file else 'The text'} is too long to translate at once "
            f"({len(text):,} characters; up to about {MAX_PARTS * CHUNK_CHARS:,}). Ask the "
            "person which part to translate."
        )
    choice = choose(params.source, params.target, added, partial(model_download.on_disk, ctx))
    entry, why = choice.entry, choice.why
    if entry is not None:
        try:
            translation = await translate_parts(ctx, entry, params.source, params.target, parts)
        except (NotAvailable, RunnerError) as exc:
            # The language's model couldn't start (too big for the memory left, or its server
            # didn't): the chat model translates instead, and says why.
            reason = (str(exc).splitlines() or [""])[0] or type(exc).__name__
            entry, why = None, f"{choice.entry.title} couldn't start: {reason}"
    if entry is None:
        translation = await translate_parts(ctx, None, params.source, params.target, parts)
    by = entry.title if entry is not None else "the chat model"
    said = (
        f"Translated from {name(params.source)} into {name(params.target)} by {by}"
        + (f" ({why})" if why else "")
        + ". The person sees the translation as your answer."
    )
    # Translated by the chat model: the chat suggests adding a language that has a model of its
    # own on this computer, under the tool's line (stored with it, so it stays after a reload).
    suggest = (
        None
        if entry is not None or choice.waiting
        else _suggestion(params.source, params.target, added, ctx)
    )
    hint = [{"kind": "language", "code": suggest}] if suggest else []
    if source_file is None:
        return ToolResult(said, refs=hint, answer=translation)
    saved = await run_in_threadpool(_save, ctx, source_file, params.target, translation)
    tab = {"kind": "artifact", "id": saved.id}
    return ToolResult(
        f"Translated {source_file.path} from {name(params.source)} into {name(params.target)} "
        f"by {by}" + (f" ({why})" if why else "") + f", and saved it as {saved.path}. It is "
        "open in a tab; tell the person in a sentence.",
        refs=[_file_ref(saved), *hint],
        action={"type": "open_tab", "tab": tab},
    )


def _suggestion(source: str, target: str, added: tuple[Added, ...], ctx: ToolContext) -> str | None:
    """The language worth adding for this translation: one not added yet with a model on this
    computer that translates it, and the other language too, better than the chat model. The
    target first, since that's the text written; English last, since every translation model
    has it and the other language is the one asked about."""
    already = {code for code, *_ in added}
    chat = chat_model(ctx.runtime)
    for code, other in sorted(((target, source), (source, target)), key=lambda p: p[0] == "en"):
        offered = options(code, ctx.runtime.tier, chat)
        if code not in already and any(o.entry.translate.covers(other) for o in offered):
            return code
    return None


def translate_tool(added: tuple[Added, ...]) -> AgentTool:
    """The tool for one turn, with the languages added when the turn began."""
    return AgentTool(
        name="translate",
        description=(
            "Translate text, or a project file, into another language, on this computer. Use "
            "it whenever the person asks for a translation; don't translate yourself. Text's "
            "translation is shown to the person as your answer; a file's is saved as a new "
            "file beside it."
        ),
        params=TranslateParams,
        run=partial(_translate, added),
        max_tokens=200,
    )
