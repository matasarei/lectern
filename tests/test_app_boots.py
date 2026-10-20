from fastapi.testclient import TestClient
from pydantic import BaseModel

from lectern import __version__
from lectern.agent.tools import AgentTool, ToolResult
from lectern.app import create_app
from lectern.core import settings
from lectern.modules import ToolEnv
from lectern.modules.web import WebModule


def test_health_answers(paths):
    # The security layer only answers on 127.0.0.1/localhost, so the client must use that host.
    client = TestClient(create_app(paths), base_url="http://127.0.0.1:8765")

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": __version__}


def test_home_comes_from_env(paths):
    from lectern.config import get_paths

    assert get_paths().home == paths.home


def test_the_chat_gets_the_export_tools_over_the_installed_exporters(paths):
    app = create_app(paths)
    names = [t.name for t in app.state.turns._registry().all()]
    assert {"export.formats", "export.run"} <= set(names)


def test_a_turn_after_web_search_is_switched_on_gets_the_web_tools(paths, monkeypatch):
    tool = AgentTool(
        name="web.demo", description="A demo tool.", params=BaseModel,
        run=lambda ctx, p: ToolResult("ok"),
    )  # fmt: skip
    monkeypatch.setattr(WebModule, "tools", lambda self, env: [tool] if env.web else [])
    app = create_app(paths)
    assert app.state.turns._registry().get("web_demo") is None  # off by default

    settings.put(app.state.sessions, settings.WEB_KEY, {"enabled": True})
    assert app.state.turns._registry().get("web_demo") is tool

    settings.put(app.state.sessions, settings.WEB_KEY, {"enabled": False})
    assert app.state.turns._registry().get("web_demo") is None


def test_the_web_switch_leaves_the_rest_of_the_env_as_the_probe_found_it(paths, monkeypatch):
    seen = []
    monkeypatch.setattr(WebModule, "tools", lambda self, env: seen.append(env) or [])
    app = create_app(paths)
    monkeypatch.setattr(app.state, "tool_env", lambda: ToolEnv(sandbox=True))
    settings.put(app.state.sessions, settings.WEB_KEY, {"enabled": True})
    app.state.turns._registry()
    assert seen == [ToolEnv(sandbox=True, web=True)]
