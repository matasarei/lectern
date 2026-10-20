"""The Web module: searching the web and Wikipedia, and opening a page into the project, each
on the person's Allow. Off until an admin switches it on in Models & memory (the `web`
setting); its tools are offered from the turn after that."""

from __future__ import annotations

from lectern.agent.tools import AgentTool
from lectern.modules import ToolEnv
from lectern.modules.web.tools import WEB_TOOLS


class WebModule:
    id = "web"

    def tools(self, env: ToolEnv) -> list[AgentTool]:
        if not env.web:
            return []
        return list(WEB_TOOLS)


module = WebModule()
