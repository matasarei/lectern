"""One chat turn: the model answers or calls tools, the tools run, and it goes round again.

The `main` model is leased only while it's generating. Tools run without it, and a
confirmation card waits without it, so a job (M2) can use the GPU while the person decides.

Everything the page needs arrives as events (see `TurnRun.emit`); everything worth keeping
is stored as messages, so a reload shows the same chat.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx2
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from lectern.agent import compaction, state
from lectern.agent.context import (
    Attachment,
    Built,
    ContextTooSmall,
    Counter,
    answer_reserve,
    build,
    split_turns,
)
from lectern.agent.prompts import EXACT_CHECK, system_prompt
from lectern.agent.state import ChatState
from lectern.agent.tools import Registry, ToolContext, ToolError, ToolResult
from lectern.core.auth import User
from lectern.downloads.fetch import DownloadError
from lectern.projects import search
from lectern.projects.store import Project
from lectern.projects.tables import ArtifactRow, ConfirmationRow, MessageRow, SessionRow, now
from lectern.runners.base import RunnerError
from lectern.runtime.supervisor import ModelRuntime, NotAvailable, Priority

MAX_MODEL_CALLS = 8
TEMPERATURE = 0.6
RESULT_PREVIEW = 600  # characters of a tool result shown in the chat line
# A reply held for the exact-results check streams once it's longer than this: an essay
# isn't a bare sum (the longest sum answered from memory in the calc set was a few hundred).
HOLD_CHARS = 1500


@dataclass
class TurnRun:
    """What a turn needs. `decide` waits for the person's answer to a confirmation card."""

    project: Project
    sessions: object  # sessionmaker for the project database
    user: User
    session_id: int
    runtime: ModelRuntime
    registry: Registry
    emit: Callable[[dict], None]
    decide: Callable[[int], asyncio.Future[bool]]
    turn_state: Callable[[int], str] = lambda _sid: "idle"
    downloads: object = None  # passed on to tools (the coding model's download)
    sandbox: object = None  # passed on to tools (the Sandbox module)
    ocr_jobs: object = None  # passed on to tools (the OCR module)
    stats: dict = field(default_factory=dict)
    # The chat's token counts, by model: kept from call to call and turn to turn
    token_counts: dict[str, dict[str, int]] = field(default_factory=dict)


class TurnFailed(Exception):
    """Ends the turn with a message for the person."""


# --- the transcript ------------------------------------------------------------------------


@dataclass
class History:
    """What the next model call is built from."""

    messages: list[dict]  # the turns after the summary, oldest first (each with its "id")
    attachments: list[Attachment]
    state: ChatState


def load_history(run: TurnRun) -> History:
    """The transcript after the summary (all of it when there is none), the files attached
    anywhere in this chat, and the chat's state record."""
    chat_state = state.load(run.sessions, run.session_id)  # type: ignore[arg-type]
    with run.sessions() as session:
        rows = session.scalars(
            select(MessageRow)
            .where(MessageRow.session_id == run.session_id)
            .order_by(MessageRow.id)
        ).all()
        attached: list[int] = []
        for row in rows:
            for artifact_id in json.loads(row.attachments or "[]"):
                if artifact_id not in attached:
                    attached.append(artifact_id)
        attachments = []
        for artifact_id in attached:
            artifact = session.get(ArtifactRow, artifact_id)
            if artifact is None or artifact.trashed_at is not None:
                continue
            chunks = search.chunks(session, artifact_id)
            text = "\n\n".join(
                f"### {c.heading}\n{c.body}" if c.heading else c.body for c in chunks
            )
            attachments.append(Attachment(artifact_id, artifact.path, text, artifact.text_note))
    recent = [r for r in rows if r.id > chat_state.summary_upto]
    return History([as_openai(r) for r in recent], attachments, chat_state)


def as_openai(row: MessageRow) -> dict:
    if row.role == "tool":
        return {
            "id": row.id,
            "role": "tool",
            "tool_call_id": row.tool_call_id,
            "content": row.content,
            "name": row.tool_name,
        }
    message: dict = {"id": row.id, "role": row.role, "content": row.content}
    if row.tool_calls:
        message["tool_calls"] = json.loads(row.tool_calls)
    return message


def store_message(run: TurnRun, **fields: object) -> int:
    with run.sessions.begin() as session:
        row = MessageRow(session_id=run.session_id, **fields)
        session.add(row)
        chat = session.get(SessionRow, run.session_id)
        if chat is not None:
            chat.updated_at = now()
        session.flush()
        return row.id


# --- the model call ------------------------------------------------------------------------


@dataclass
class Reply:
    content: str = ""
    calls: list[dict] = field(default_factory=list)  # OpenAI tool_calls
    tokens: int = 0
    first_token_at: float | None = None
    tps: float | None = None
    shown: bool = False  # a held reply that grew past HOLD_CHARS was streamed after all


async def call_model(run: TurnRun, history: History) -> Reply:
    async with run.runtime.lease("main", Priority.CHAT) as lease:
        if not run.stats.get("model_sent"):
            run.emit({"type": "model", "model_id": lease.model_id, "ctx": lease.plan.ctx})
            run.stats["model_sent"] = True
        built = await build_context(run, history, lease.plan.ctx, _counter(run, lease))
        if built.left_out or built.released or built.stubbed_files or built.pins_left_out:
            run.emit(
                {
                    "type": "context",
                    "left_out": built.left_out,
                    "released": built.released,
                    "stubbed_files": built.stubbed_files,
                    "pins_left_out": built.pins_left_out,
                }
            )
        run.emit({"type": "usage", "parts": built.parts, "budget": built.budget})
        body = {
            "messages": built.messages,
            "tools": run.registry.schemas(),
            "stream": True,
            "stream_options": {"include_usage": True},
            "max_tokens": answer_reserve(lease.plan.ctx),
            "temperature": TEMPERATURE,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        return await _stream(run, lease.client, body)


async def build_context(run: TurnRun, history: History, ctx: int, counter: Counter) -> Built:
    try:
        return await build(
            system=system_prompt(
                run.project.name, sandbox=run.registry.get("code_run") is not None
            ),
            tools=run.registry.schemas(),
            attachments=history.attachments,
            history=history.messages,
            ctx=ctx,
            counter=counter,
            pins=[(p.kind, p.text) for p in history.state.pinned],
            summary=history.state.summary,
            followups=[f.label for f in history.state.followups if f.tool],  # not notices
        )
    except ContextTooSmall as exc:
        raise TurnFailed(str(exc)) from None


# --- compaction, after a turn ----------------------------------------------------------------


async def maybe_compact(run: TurnRun) -> None:
    """If the next call would be over COMPACT_AT of the context, fold the oldest turns into
    the summary. Never fails the turn: a problem is reported and the old summary stays."""
    try:
        history = await run_in_threadpool(load_history, run)
        async with run.runtime.lease("main", Priority.CHAT) as lease:
            counter = _counter(run, lease)
            built = await build_context(run, history, lease.plan.ctx, counter)
            used = sum(built.parts.values())
            if used <= compaction.COMPACT_AT * built.budget and not built.left_out:
                return
            turns = split_turns(history.messages)
            sizes = [sum([await counter.message(m) for m in turn]) for turn in turns]
            fixed = used - built.parts["turns"] - built.parts["summary"]
            folded_count = compaction.fold_point(sizes, fixed, built.budget)
            if folded_count == 0:
                return
            folded = [m for turn in turns[:folded_count] for m in turn]
            run.emit({"type": "compacting"})
            summary = await compaction.write_summary(lease.client, history.state.summary, folded)
            summary_tokens = await counter.count(summary)  # while the runner is still ours
        await run_in_threadpool(
            state.save_summary, run.sessions, run.session_id, summary, folded[-1]["id"]
        )
        run.emit(
            {
                "type": "compacted",
                "summarised": len(folded),
                "before": used,
                "after": fixed + summary_tokens + sum(sizes[folded_count:]),
                "budget": built.budget,
            }
        )
    except (
        compaction.SummaryFailed,
        TurnFailed,
        NotAvailable,
        RunnerError,
        httpx2.HTTPError,
    ) as exc:
        run.emit(
            {
                "type": "compaction_failed",
                "detail": str(exc).splitlines()[0] if str(exc) else type(exc).__name__,
            }
        )


def _counter(run: TurnRun, lease) -> Counter:  # noqa: ANN001 — a runtime lease
    """A counter on the leased model's tokenizer, remembering into the chat's counts for that
    model (another model's tokenizer counts differently)."""
    return Counter(_tokenizer(lease.client), run.token_counts.setdefault(lease.model_id, {}))


def _tokenizer(client: httpx2.AsyncClient):  # noqa: ANN202
    """The runner's /tokenize (llama-server); None-returning fallback when it has none."""
    state = {"works": True}

    async def tokenize(text: str) -> int:
        if state["works"]:
            try:
                response = await client.post("/tokenize", json={"content": text})
                if response.status_code == 200:
                    return len(response.json()["tokens"])
            except (httpx2.HTTPError, ValueError, KeyError):
                pass
            state["works"] = False  # e.g. MLX: estimate from here on
        return len(text) // 3 + 1

    return tokenize


async def _stream(run: TurnRun, client: httpx2.AsyncClient, body: dict) -> Reply:
    reply = Reply()
    calls: dict[int, dict] = {}
    async with client.stream("POST", "/v1/chat/completions", json=body) as response:
        if response.status_code != 200:
            detail = (await response.aread()).decode(errors="replace")[:500]
            raise TurnFailed(f"Model error: {detail}")
        async for line in response.aiter_lines():
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            try:
                chunk = json.loads(line.removeprefix("data: "))
            except json.JSONDecodeError:
                raise TurnFailed("The model server sent a malformed reply.") from None
            usage = chunk.get("usage") or {}
            if usage.get("completion_tokens"):
                reply.tokens = int(usage["completion_tokens"])
            timings = chunk.get("timings") or {}
            if timings.get("predicted_per_second"):
                reply.tps = float(timings["predicted_per_second"])
            for choice in chunk.get("choices") or []:
                delta = choice.get("delta") or {}
                if delta.get("content"):
                    if reply.first_token_at is None:
                        reply.first_token_at = time.perf_counter()
                    reply.content += delta["content"]
                    run.emit({"type": "delta", "text": delta["content"]})
                for part in delta.get("tool_calls") or []:
                    slot = calls.setdefault(
                        int(part.get("index", 0)),
                        {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
                    )
                    if part.get("id"):
                        slot["id"] = part["id"]
                    function = part.get("function") or {}
                    slot["function"]["name"] += function.get("name") or ""
                    slot["function"]["arguments"] += function.get("arguments") or ""
                    if reply.first_token_at is None:
                        reply.first_token_at = time.perf_counter()
    if reply.tps is None and reply.tokens > 1 and reply.first_token_at is not None:
        # A server that reports no speed of its own (MLX's): the tokens after the first, over
        # the time since it arrived.
        elapsed = time.perf_counter() - reply.first_token_at
        if elapsed > 0:
            reply.tps = (reply.tokens - 1) / elapsed
    reply.calls = [calls[i] for i in sorted(calls)]
    for i, call in enumerate(reply.calls):
        call["id"] = call["id"] or f"call_{int(time.time() * 1000)}_{i}"
    return reply


# --- tools ---------------------------------------------------------------------------------


async def run_tool(run: TurnRun, call: dict, failures: dict[str, int]) -> ToolResult:
    """Validate, confirm when the tool asks first, run. Raises TurnFailed on a second
    invalid call in this turn."""
    function = call["function"]
    tool = run.registry.get(function["name"])
    shown = tool.name if tool else function["name"]
    run.emit(
        {"type": "tool", "call_id": call["id"], "name": shown, "arguments": function["arguments"]}
    )
    if tool is None:
        known = ", ".join(t.wire_name for t in run.registry.all())
        return ToolResult(f"Error: there is no tool {function['name']!r}. Tools: {known}.")
    try:
        params = tool.validate(function["arguments"])
    except ToolError as exc:
        failures[tool.name] = failures.get(tool.name, 0) + 1
        if failures[tool.name] > 1:
            raise TurnFailed(str(exc)) from None
        return ToolResult(f"Error: {exc} Fix the arguments and call it again.")

    async def ask(question: str) -> bool:
        return await _confirm(run, call, tool.name, question, function["arguments"])

    context = ToolContext(
        project=run.project,
        sessions=run.sessions,  # type: ignore[arg-type]
        user=run.user,
        session_id=run.session_id,
        turn_state=run.turn_state,
        confirm=ask,
        emit=lambda event: run.emit({**event, "call_id": call["id"]}),
        runtime=run.runtime,
        downloads=run.downloads,
        sandbox=run.sandbox,
        ocr_jobs=run.ocr_jobs,
        registry=run.registry,
    )
    if tool.confirm:
        try:
            question = (
                await run_in_threadpool(tool.confirm_text, context, params)
                if tool.confirm_text
                else f"Run {tool.name}?"
            )
        except ToolError as exc:
            return ToolResult(f"Error: {exc}")
        approved = await _confirm(run, call, tool.name, question, function["arguments"])
        if not approved:
            return ToolResult(f"The person declined: {tool.name} was not run. Don't retry it.")
    try:
        if inspect.iscoroutinefunction(tool.run):
            return await tool.run(context, params)
        return await run_in_threadpool(tool.run, context, params)
    except ToolError as exc:
        return ToolResult(f"Error: {exc}")


async def _confirm(run: TurnRun, call: dict, name: str, text: str, arguments: str) -> bool:
    def create() -> int:
        with run.sessions.begin() as session:
            row = ConfirmationRow(
                session_id=run.session_id, tool_call_id=call["id"], tool=name, arguments=arguments
            )
            session.add(row)
            session.flush()
            return row.id

    confirmation_id = await run_in_threadpool(create)
    future = run.decide(confirmation_id)
    run.emit(
        {
            "type": "confirm",
            "confirmation_id": confirmation_id,
            "call_id": call["id"],
            "name": name,
            "text": text,
        }
    )
    try:
        # The model is idle while the person reads the card: a job held for the chat may go
        async with run.runtime.waiting_for_person():
            approved = await future
    except asyncio.CancelledError:
        await run_in_threadpool(_record_decision, run, confirmation_id, False)
        raise
    await run_in_threadpool(_record_decision, run, confirmation_id, approved)
    run.emit({"type": "confirmed", "confirmation_id": confirmation_id, "approved": approved})
    return approved


def _record_decision(run: TurnRun, confirmation_id: int, approved: bool) -> None:
    with run.sessions.begin() as session:
        row = session.get(ConfirmationRow, confirmation_id)
        if row is not None and row.status == "pending":
            row.status = "approved" if approved else "declined"
            row.decided_at = now()


# --- the turn ------------------------------------------------------------------------------


async def run_turn(run: TurnRun) -> str:
    """Runs until the model answers without calling a tool. Returns "done", "error" or
    "stopped"; a stop (cancel) keeps what was written so far."""
    started = time.perf_counter()
    first_token: float | None = None
    tokens = 0
    tps: float | None = None
    failures: dict[str, int] = {}
    calls_left = MAX_MODEL_CALLS
    called_a_tool = False
    checked = False
    try:
        while calls_left:
            calls_left -= 1
            history = await run_in_threadpool(load_history, run)
            # A reply that could be a sum from memory isn't shown until it's known not to be
            hold = not called_a_tool and not checked and _may_check(run, history)
            reply = await _call(run, history, hold=hold, release_after=HOLD_CHARS)
            first_token = first_token or reply.first_token_at
            tokens += reply.tokens
            tps = reply.tps or tps
            if (
                hold
                and not reply.calls
                and not reply.shown
                and reply.content.strip()
                and calls_left
            ):
                checked = True
                calls_left -= 1
                check = await _call(run, _with_check(history, reply.content), hold=True)
                tokens += check.tokens
                tps = check.tps or tps
                if check.calls:  # it was an exact result: computed now, the draft dropped
                    reply = check
            if hold and not reply.shown and _show(run, reply.content):
                first_token = first_token or time.perf_counter()  # when it was seen
            message_id = await run_in_threadpool(
                lambda r=reply: store_message(
                    run,
                    role="assistant",
                    content=r.content,
                    tool_calls=json.dumps(r.calls) if r.calls else None,
                )
            )
            run.stats["partial"] = ""  # stored
            run.emit({"type": "assistant", "message_id": message_id, "tool_calls": reply.calls})
            if not reply.calls:
                run.emit(
                    {
                        "type": "done",
                        "ttft": round(first_token - started, 3) if first_token else None,
                        "tps": round(tps, 1) if tps else None,
                        "tokens": tokens,
                    }
                )
                try:
                    await maybe_compact(run)
                except asyncio.CancelledError:
                    # Stop while old turns were being folded: the answer is stored and stands,
                    # and the summary is made after a later turn. The task is no longer being
                    # cancelled, and says so (a timeout or task group around it reads that).
                    if (task := asyncio.current_task()) is not None:
                        task.uncancel()
                    run.emit({"type": "compaction_failed", "detail": "stopped"})
                return "done"
            called_a_tool = True
            for call in reply.calls:
                result = await run_tool(run, call, failures)
                await run_in_threadpool(
                    lambda c=call, r=result: store_message(
                        run,
                        role="tool",
                        content=r.text,
                        tool_call_id=c["id"],
                        tool_name=(run.registry.get(c["function"]["name"]) or _Unknown).name,
                        refs=json.dumps(r.refs) if r.refs else None,
                    )
                )
                run.emit(
                    {
                        "type": "tool_result",
                        "call_id": call["id"],
                        "text": result.text[:RESULT_PREVIEW],
                        "error": result.text.startswith("Error:"),
                        "refs": result.refs,
                        "action": result.action,
                    }
                )
        raise TurnFailed(
            f"Stopped after {MAX_MODEL_CALLS} model calls without an answer. Ask again, "
            "or split the request."
        )
    except TurnFailed as exc:
        run.emit({"type": "error", "detail": str(exc)})
        return "error"
    except NotAvailable as exc:
        run.emit({"type": "error", "detail": str(exc)})
        return "error"
    except (RunnerError, DownloadError) as exc:
        run.emit({"type": "error", "detail": str(exc).splitlines()[0]})
        return "error"
    except httpx2.HTTPError as exc:
        run.emit({"type": "error", "detail": f"Lost the model server: {exc}"})
        return "error"
    except asyncio.CancelledError:
        partial = run.stats.get("partial", "")
        if partial:
            await asyncio.shield(
                run_in_threadpool(lambda: store_message(run, role="assistant", content=partial))
            )
        run.emit({"type": "stopped"})
        return "stopped"


class _Unknown:
    name = "unknown"


async def _call(
    run: TurnRun, history: History, *, hold: bool = False, release_after: int | None = None
) -> Reply:
    """call_model, keeping the text streamed so far in `stats["partial"]`, so a stop in the
    middle of a reply can still store it. `hold`: the reply's text isn't streamed (the loop
    shows it once it's checked), and a stop meanwhile stores none of it: nobody saw it.
    `release_after`: a held reply longer than that many characters streams after all, from
    the start, and comes back `shown`."""
    original = run.emit
    run.stats["partial"] = ""
    held: list[str] = []
    released = False
    released_at: float | None = None

    def emit(event: dict) -> None:
        nonlocal released, released_at
        if event.get("type") == "delta":
            if hold and not released:
                held.append(event["text"])
                if release_after is None or sum(map(len, held)) <= release_after:
                    return
                released, released_at = True, time.perf_counter()
                event = {**event, "text": "".join(held)}
            run.stats["partial"] += event["text"]
        original(event)

    run.emit = emit
    try:
        reply = await call_model(run, history)
    finally:
        run.emit = original
    reply.shown = released
    if hold:  # its first token is when the person first saw text, not when it was written
        reply.first_token_at = released_at
    return reply


# --- the exact-results check (docs/acceptance/m1b.md) -------------------------------------


def _may_check(run: TurnRun, history: History) -> bool:
    """A reply to the person's message could state a sum from memory: the sandbox is running
    (its tools are offered only then), the turn answers that message itself (not a follow-up
    after a tool, whose person's message is an older one), and it has a number in it."""
    if run.registry.get("code_run") is None or not history.messages:
        return False
    asked = history.messages[-1]
    return asked["role"] == "user" and re.search(r"\d", asked["content"] or "") is not None


def _with_check(history: History, draft: str) -> History:
    """The turn so far, the held draft, and the question whether it states an exact result.
    Neither is stored: the draft is the answer only if the check says there's nothing to
    compute (NO_CALC, or anything but a tool call)."""
    asked = [
        {"id": None, "role": "assistant", "content": draft},
        {"id": None, "role": "user", "content": EXACT_CHECK},
    ]
    return History([*history.messages, *asked], history.attachments, history.state)


def _show(run: TurnRun, text: str) -> bool:
    """A held reply's text, once it stands: all at once. Whether there was any."""
    if text:
        run.emit({"type": "delta", "text": text})
    return bool(text)
