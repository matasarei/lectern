"""One chat turn: the model answers or calls tools, the tools run, and it goes round again.

The `main` model is leased only while it's generating. Tools run without it, and a
confirmation card waits without it, so a job (M2) can use the GPU while the person decides.

Everything the page needs arrives as events (see `TurnRun.emit`); everything worth keeping
is stored as messages, so a reload shows the same chat.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx2
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from lectern.agent.context import Attachment, ContextTooSmall, Counter, answer_reserve, build
from lectern.agent.prompts import system_prompt
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
    stats: dict = field(default_factory=dict)


class TurnFailed(Exception):
    """Ends the turn with a message for the person."""


# --- the transcript ------------------------------------------------------------------------


def load_history(run: TurnRun) -> tuple[list[dict], list[Attachment]]:
    """The whole transcript in OpenAI form, and the files attached anywhere in this chat."""
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
    return [as_openai(r) for r in rows], attachments


def as_openai(row: MessageRow) -> dict:
    if row.role == "tool":
        return {
            "role": "tool",
            "tool_call_id": row.tool_call_id,
            "content": row.content,
            "name": row.tool_name,
        }
    message: dict = {"role": row.role, "content": row.content}
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


async def call_model(run: TurnRun, history: list[dict], attachments: list[Attachment]) -> Reply:
    async with run.runtime.lease("main", Priority.CHAT) as lease:
        if not run.stats.get("model_sent"):
            run.emit({"type": "model", "model_id": lease.model_id, "ctx": lease.plan.ctx})
            run.stats["model_sent"] = True
        counter = Counter(_tokenizer(lease.client))
        try:
            built = await build(
                system=system_prompt(run.project.name),
                tools=run.registry.schemas(),
                attachments=attachments,
                history=history,
                ctx=lease.plan.ctx,
                counter=counter,
            )
        except ContextTooSmall as exc:
            raise TurnFailed(str(exc)) from None
        if built.left_out or built.released or built.stubbed_files:
            run.emit(
                {
                    "type": "context",
                    "left_out": built.left_out,
                    "released": built.released,
                    "stubbed_files": built.stubbed_files,
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
    if tool.confirm:
        question = tool.confirm_text(params) if tool.confirm_text else f"Run {tool.name}?"
        approved = await _confirm(run, call, tool.name, question, function["arguments"])
        if not approved:
            return ToolResult(f"The person declined: {tool.name} was not run. Don't retry it.")
    context = ToolContext(
        project=run.project,
        sessions=run.sessions,  # type: ignore[arg-type]
        user=run.user,
        session_id=run.session_id,
        turn_state=run.turn_state,
    )
    try:
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
    try:
        for _ in range(MAX_MODEL_CALLS):
            history, attachments = await run_in_threadpool(load_history, run)
            reply = await _call(run, history, attachments)
            first_token = first_token or reply.first_token_at
            tokens += reply.tokens
            tps = reply.tps or tps
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
                return "done"
            for call in reply.calls:
                result = await run_tool(run, call, failures)
                await run_in_threadpool(
                    lambda c=call, r=result: store_message(
                        run,
                        role="tool",
                        content=r.text,
                        tool_call_id=c["id"],
                        tool_name=(run.registry.get(c["function"]["name"]) or _Unknown).name,
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


async def _call(run: TurnRun, history: list[dict], attachments: list[Attachment]) -> Reply:
    """call_model, keeping the text streamed so far in `stats["partial"]`, so a stop in the
    middle of a reply can still store it."""
    original = run.emit
    run.stats["partial"] = ""

    def emit(event: dict) -> None:
        if event.get("type") == "delta":
            run.stats["partial"] += event["text"]
        original(event)

    run.emit = emit
    try:
        return await call_model(run, history, attachments)
    finally:
        run.emit = original
