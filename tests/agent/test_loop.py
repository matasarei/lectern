from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx2
import pytest
from sqlalchemy import select

from lectern.agent import loop
from lectern.agent.core_tools import core_registry
from lectern.agent.turns import Busy, TurnManager
from lectern.core.auth import LOCAL_USER
from lectern.hardware.tiers import Tier
from lectern.projects import files
from lectern.projects.tables import ConfirmationRow, MessageRow, SessionRow
from lectern.runners.base import MeasuredMemory
from lectern.runtime.planner import ModelShape
from lectern.runtime.supervisor import ModelChoice, ModelRuntime

GIB = 1024**3
SHAPE = ModelShape(layers=8, kv_layers=8, kv_heads=1, key_length=64, value_length=64)


def sse(*chunks: dict) -> bytes:
    return "".join(
        [f"data: {json.dumps(c, ensure_ascii=False)}\n\n" for c in chunks] + ["data: [DONE]\n\n"]
    ).encode()


def text_reply(*pieces: str) -> bytes:
    return sse(
        *[{"choices": [{"delta": {"content": p}}]} for p in pieces],
        {"choices": [], "usage": {"completion_tokens": len(pieces)}},
    )


def tool_reply(name: str, arguments: dict | str, call_id: str = "call_1") -> bytes:
    args = arguments if isinstance(arguments, str) else json.dumps(arguments, ensure_ascii=False)
    head, tail = args[: len(args) // 2], args[len(args) // 2 :]
    return sse(
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": call_id, "type": "function",
             "function": {"name": name, "arguments": head}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": tail}}]}}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    )  # fmt: skip


class FakeHandle:
    """A model server that plays back one prepared reply per completion request."""

    model_id = "qwen3.5-4b"
    base_url = "http://127.0.0.1:1"
    pid = 4242

    def __init__(self, replies: list[bytes], *, hold: asyncio.Event | None = None) -> None:
        self.replies = list(replies)
        self.requests: list[dict] = []
        self.hold = hold

    def client(self) -> httpx2.AsyncClient:
        async def handler(request: httpx2.Request) -> httpx2.Response:
            await asyncio.sleep(0)
            body = json.loads(request.content)
            if request.url.path == "/tokenize":
                return httpx2.Response(200, json={"tokens": [0] * (len(body["content"]) // 4 + 1)})
            self.requests.append(body)
            if self.hold is not None:
                await self.hold.wait()
            return httpx2.Response(200, content=self.replies.pop(0))

        return httpx2.AsyncClient(base_url=self.base_url, transport=httpx2.MockTransport(handler))

    def is_running(self) -> bool:
        return True

    def measured_memory(self) -> MeasuredMemory:
        return MeasuredMemory(device_bytes=2 * GIB)

    async def stop(self) -> None:
        pass


class FakeRunner:
    def __init__(self, handle):  # noqa: ANN001
        self.handle = handle

    async def start(self, spec):  # noqa: ANN001
        return self.handle


def make_runtime(handle: FakeHandle) -> ModelRuntime:
    return ModelRuntime(
        budget=8 * GIB,
        unified=False,
        tier=Tier.MINIMUM,
        resolve=lambda role: ModelChoice(
            role, "qwen3.5-4b", Path("/m/q.gguf"), 2 * GIB, SHAPE, 8192
        ),
        runner_for=lambda choice: FakeRunner(handle),
    )


@pytest.fixture
def setup(store):
    def make(replies, **kwargs):
        handle = FakeHandle(replies, **kwargs)
        runtime = make_runtime(handle)
        manager = TurnManager(runtime, store, core_registry())
        project = store.create(LOCAL_USER, "Lease archive")
        return manager, project, handle, runtime

    return make


def add_file(store, project, name, text):
    with store.db(project).begin() as session:
        return files.write_text(
            project.root, session, owner_id=LOCAL_USER.id, name=name, text=text,
            origin=files.Origin("import"),
        ).id  # fmt: skip


def transcript(store, project):
    with store.db(project)() as session:
        rows = session.scalars(select(MessageRow).order_by(MessageRow.id)).all()
    return [(r.role, r.content, r.tool_name) for r in rows]


async def turn(manager, project, text, attachments=(), session_id=1):
    await manager.send(project, LOCAL_USER, session_id, text, list(attachments))
    return await manager.wait((project.id, session_id))


def events(manager, project, session_id=1):
    return manager.channel((project.id, session_id)).events


def test_a_tool_call_then_an_answer(setup, store):
    manager, project, handle, _ = setup(
        [tool_reply("project_find", {"query": "lease"}), text_reply("It's ", "lease.md.")]
    )
    add_file(store, project, "lease.md", "# Terms\nPay by the 10th.")

    status = asyncio.run(turn(manager, project, "Is there a lease file?"))

    assert status == "done"
    kinds = [e["type"] for e in events(manager, project)]
    assert kinds == [
        "turn", "model", "usage", "assistant", "tool", "tool_result",
        "usage", "delta", "delta", "assistant", "done", "turn",
    ]  # fmt: skip
    tool = next(e for e in events(manager, project) if e["type"] == "tool")
    assert (tool["name"], json.loads(tool["arguments"])) == ("project.find", {"query": "lease"})
    result = next(e for e in events(manager, project) if e["type"] == "tool_result")
    # The chat is already named after the message, so it matches "lease" too.
    assert "lease.md · file" in result["text"]
    assert [r["name"] for r in result["refs"]] == ["Is there a lease file?", "lease.md"]
    assert [(r, c[:12], n) for r, c, n in transcript(store, project)] == [
        ("user", "Is there a l", None),
        ("assistant", "", None),
        ("tool", "Is there a l", "project.find"),
        ("assistant", "It's lease.m", None),
    ]
    # The second request carried the tool result, and the tools went with every request.
    second = handle.requests[1]
    assert second["messages"][-1]["role"] == "tool"
    assert second["messages"][-2]["tool_calls"][0]["function"]["name"] == "project_find"
    assert {t["function"]["name"] for t in second["tools"]} >= {"project_find", "document_read"}
    assert [e["seq"] for e in events(manager, project)] == list(range(1, 13))


def test_the_chat_and_an_untitled_project_are_named_from_the_first_message(setup, store):
    manager, _, _, _ = setup([text_reply("Hi")])
    project = store.create(LOCAL_USER)  # "Untitled project"
    asyncio.run(turn(manager, project, "Who are the parties in the 1998 lease contract?"))

    with store.db(project)() as session:
        assert session.get(SessionRow, 1).title == "Who are the parties in the…"
    assert store.get(LOCAL_USER, project.id).name == "Who are the parties in the…"


def test_trash_waits_for_allow_without_holding_the_model(setup, store):
    manager, project, _, runtime = setup(
        [tool_reply("artifact_trash", {"name": "old.md"}), text_reply("Done.")]
    )
    add_file(store, project, "old.md", "old")

    async def scenario():
        await manager.send(project, LOCAL_USER, 1, "Delete old.md", [])
        key = (project.id, 1)
        for _ in range(200):
            await asyncio.sleep(0.01)
            if manager.state(key) == "waiting":
                break
        assert manager.state(key) == "waiting"
        assert (project.root / "old.md").exists()
        assert all(r["leases"] == 0 for r in runtime.state()["residents"])
        card = next(e for e in events(manager, project) if e["type"] == "confirm")
        assert card["text"] == "Move old.md to the trash?"
        assert manager.decide(key, card["confirmation_id"], True)
        assert not manager.decide(key, card["confirmation_id"], True)  # already answered
        return await manager.wait(key)

    assert asyncio.run(scenario()) == "done"
    assert not (project.root / "old.md").exists()
    with store.db(project)() as session:
        assert session.scalars(select(ConfirmationRow.status)).all() == ["approved"]


def test_dont_allow_tells_the_model_and_moves_nothing(setup, store):
    manager, project, handle, _ = setup(
        [tool_reply("artifact_trash", {"name": "old.md"}), text_reply("Kept it.")]
    )
    add_file(store, project, "old.md", "old")

    async def scenario():
        await manager.send(project, LOCAL_USER, 1, "Delete old.md", [])
        key = (project.id, 1)
        while manager.state(key) != "waiting":
            await asyncio.sleep(0.01)
        card = next(e for e in events(manager, project) if e["type"] == "confirm")
        manager.decide(key, card["confirmation_id"], False)
        return await manager.wait(key)

    assert asyncio.run(scenario()) == "done"
    assert (project.root / "old.md").exists()
    assert handle.requests[1]["messages"][-1]["content"].startswith("The person declined")


def test_bad_arguments_are_sent_back_once_then_the_turn_ends(setup, store):
    manager, project, handle, _ = setup(
        [tool_reply("project_find", {"q": "x"}), tool_reply("project_find", {"q": "y"}, "call_2")]
    )

    assert asyncio.run(turn(manager, project, "Find x")) == "error"

    retry = handle.requests[1]["messages"][-1]["content"]
    assert retry.startswith("Error: Invalid arguments for project.find: query: Field required")
    error = next(e for e in events(manager, project) if e["type"] == "error")
    assert "Invalid arguments for project.find" in error["detail"]


def test_an_unknown_tool_gets_the_list_of_tools(setup):
    manager, project, handle, _ = setup([tool_reply("shell_exec", {"cmd": "ls"}), text_reply("ok")])
    assert asyncio.run(turn(manager, project, "ls")) == "done"
    assert "there is no tool 'shell_exec'" in handle.requests[1]["messages"][-1]["content"]


def test_a_turn_stops_after_eight_model_calls(setup):
    replies = [tool_reply("project_list", {}, f"call_{i}") for i in range(8)]
    manager, project, handle, _ = setup(replies)

    assert asyncio.run(turn(manager, project, "loop")) == "error"
    assert len(handle.requests) == 8
    assert "Stopped after 8 model calls" in events(manager, project)[-2]["detail"]


def test_stop_keeps_what_was_written(setup, store):
    hold = asyncio.Event()
    manager, project, _, _ = setup([text_reply("never")], hold=hold)

    async def scenario():
        await manager.send(project, LOCAL_USER, 1, "Write a long letter", [])
        key = (project.id, 1)
        await asyncio.sleep(0.05)
        with pytest.raises(Busy):
            await manager.send(project, LOCAL_USER, 1, "again", [])
        assert await manager.stop(key)
        return manager.state(key)

    assert asyncio.run(scenario()) == "idle"
    assert [e["type"] for e in events(manager, project)][-2:] == ["stopped", "turn"]
    assert events(manager, project)[-1]["status"] == "stopped"
    assert transcript(store, project) == [("user", "Write a long letter", None)]


def test_stop_during_a_reply_stores_the_partial_text(setup, store, monkeypatch):
    manager, project, _, _ = setup([])

    async def slow_stream(run, client, body):  # noqa: ANN001
        run.emit({"type": "delta", "text": "Dear tenant, "})
        await asyncio.sleep(10)

    monkeypatch.setattr(loop, "_stream", slow_stream)

    async def scenario():
        await manager.send(project, LOCAL_USER, 1, "Write a letter", [])
        while not any(e["type"] == "delta" for e in events(manager, project)):
            await asyncio.sleep(0.01)
        await manager.stop((project.id, 1))

    asyncio.run(scenario())
    assert transcript(store, project)[-1] == ("assistant", "Dear tenant, ", None)


def test_attached_files_go_to_the_model(setup, store):
    manager, project, handle, _ = setup([text_reply("Summary.")])
    notes = add_file(store, project, "notes.md", "# Payment\nBy the 10th of each month.")

    asyncio.run(turn(manager, project, "Summarise this", attachments=[notes, 999]))

    system = handle.requests[0]["messages"][0]["content"]
    assert "## notes.md\n### Payment\nBy the 10th of each month." in system
    with store.db(project)() as session:
        user_row = session.scalars(select(MessageRow).where(MessageRow.role == "user")).one()
    assert json.loads(user_row.attachments) == [notes]  # 999 isn't a file here


def test_model_errors_end_the_turn_with_a_message(setup):
    manager, project, _, _ = setup([b"data: {not json\n\n"])
    assert asyncio.run(turn(manager, project, "hi")) == "error"
    assert events(manager, project)[-2] == {
        "type": "error", "detail": "The model server sent a malformed reply.",
        "seq": events(manager, project)[-2]["seq"],
    }  # fmt: skip
