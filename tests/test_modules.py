from __future__ import annotations

import threading

from pydantic import BaseModel

from lectern.agent.core_tools import CORE_TOOLS
from lectern.agent.tools import AgentTool, ToolResult
from lectern.hardware.sandbox import SandboxRuntime
from lectern.modules import EnvProbe, ToolEnv, load_modules, tool_registry


class _NoParams(BaseModel):
    pass


EXTRA = AgentTool(
    name="demo.thing", description="A demo tool.", params=_NoParams,
    run=lambda ctx, p: ToolResult("ok"),
)  # fmt: skip


class DemoModule:
    id = "demo"

    def tools(self, env: ToolEnv) -> list[AgentTool]:
        return [EXTRA] if env.sandbox else []


def test_the_sandbox_module_is_found_through_its_entry_point():
    assert "sandbox" in [m.id for m in load_modules()]


def test_the_ocr_module_is_found_and_brings_its_chat_tools():
    (ocr,) = [m for m in load_modules() if m.id == "ocr"]
    names = [t.name for t in ocr.tools(ToolEnv(sandbox=False))]
    assert names == ["ocr.start", "ocr.note"]  # with or without a sandbox


def test_a_modules_tools_follow_what_is_available():
    env = {"now": ToolEnv(sandbox=False)}
    build = tool_registry(CORE_TOOLS, [DemoModule()], lambda: env["now"])

    assert build().get("demo_thing") is None
    assert len(build().all()) == len(CORE_TOOLS)
    env["now"] = ToolEnv(sandbox=True)
    assert build().get("demo_thing") is EXTRA


def test_the_environment_is_checked_at_most_every_30_seconds(monkeypatch):
    calls = []
    clock = [100.0]

    def detect():
        calls.append(1)
        return SandboxRuntime(
            runtime="docker", version="28", engine_running=True, found=("docker",)
        )

    monkeypatch.setattr("lectern.modules.time.monotonic", lambda: clock[0])
    probe = EnvProbe(detect)

    assert probe() == ToolEnv(sandbox=True)
    probe()
    assert len(calls) == 1
    clock[0] += 31
    probe()
    probe.settle(5)  # the check runs in the background now
    assert len(calls) == 2


def test_a_broken_module_is_skipped(monkeypatch):
    class Broken:
        name = "broken"

        def load(self):
            raise ImportError("missing dependency")

    class Wrong:
        name = "wrong"

        def load(self):
            return object()

    monkeypatch.setattr("lectern.modules.entry_points", lambda group: [Broken(), Wrong()])
    assert load_modules() == []


def test_a_modules_routes_are_served_under_api_behind_the_user_check(paths, monkeypatch):
    from fastapi import APIRouter
    from fastapi.testclient import TestClient

    from lectern import app as app_module

    router = APIRouter()

    @router.get("/demo/hello")
    def hello() -> dict:
        return {"hello": "from a module"}

    class RoutedModule(DemoModule):
        pass

    RoutedModule.router = router
    monkeypatch.setattr(app_module, "load_modules", lambda: [RoutedModule()])
    base = "http://127.0.0.1:8765"
    app = app_module.create_app(paths)
    with TestClient(app, base_url=base) as client:
        signed = client.get("/api/demo/hello", headers={"X-Lectern-Token": app.state.token})
        anonymous = client.get("/api/demo/hello")

    assert signed.json() == {"hello": "from a module"}
    assert anonymous.status_code == 401


def test_only_modules_that_offer_exporters_add_them():
    from lectern.modules import module_exporters

    class _Export:
        id = "export-demo"
        exporter = object()

        def tools(self, env: ToolEnv) -> list[AgentTool]:
            return []

        def exporters(self) -> list:
            return [self.exporter]

    demo = _Export()
    assert module_exporters([DemoModule(), demo]) == [demo.exporter]


def test_a_stale_answer_is_given_at_once_while_it_is_checked_again(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("lectern.modules.time.monotonic", lambda: clock[0])
    release = threading.Event()
    answers = iter([True, False])

    def detect():
        running = next(answers)
        if not running:
            release.wait(10)  # the second check is slow, as `podman info` can be
        return SandboxRuntime(
            runtime="podman", version="5", engine_running=running, found=("podman",)
        )

    probe = EnvProbe(detect)
    assert probe() == ToolEnv(sandbox=True)  # the first answer: nothing to give before it
    clock[0] += 31

    turn = threading.Thread(target=lambda: seen.append(probe()))
    seen: list[ToolEnv] = []
    turn.start()
    turn.join(1.0)
    assert seen == [ToolEnv(sandbox=True)], "the turn waited for podman"
    release.set()
    probe.settle(5)
    assert probe() == ToolEnv(sandbox=False)  # the turns after it get the new answer


def test_the_first_turn_waits_only_for_the_check_already_started(monkeypatch):
    calls = []

    def detect():
        calls.append(1)
        return SandboxRuntime(runtime=None, version=None, engine_running=False, found=())

    probe = EnvProbe(detect)
    probe.start()  # as the app does when it starts
    assert probe() == ToolEnv(sandbox=False)
    probe.settle(5)
    assert len(calls) == 1  # the turn reused the startup check rather than starting another


def test_a_check_that_fails_answers_no_engine_instead_of_leaving_turns_waiting():
    def detect():
        raise RuntimeError("podman crashed")

    probe = EnvProbe(detect)
    waiting = threading.Thread(target=lambda: seen.append(probe()))
    seen: list[ToolEnv] = []
    waiting.start()
    waiting.join(5)
    assert seen == [ToolEnv(sandbox=False)]
