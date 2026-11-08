"""Translation: text translated on this computer by the model of a language an admin added
(the Languages page), or by the chat model (tools.py: `translate`)."""

from __future__ import annotations

from lectern.agent.tools import AgentTool
from lectern.modules import ToolEnv
from lectern.modules.translate.tools import translate_tool


class TranslateModule:
    id = "translate"

    def tools(self, env: ToolEnv) -> list[AgentTool]:
        return [translate_tool(env.languages)]


module = TranslateModule()
