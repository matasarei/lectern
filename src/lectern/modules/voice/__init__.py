"""Voice: the chat's replies read aloud on this computer, by a Piper voice for the reply's
language (speak.py, language.py). The model gets no tool: reading is the person's, from the UI."""

from __future__ import annotations

from lectern.agent.tools import AgentTool
from lectern.modules import ToolEnv
from lectern.modules.voice.api import router


class VoiceModule:
    id = "voice"
    router = router  # /api/voice

    def tools(self, env: ToolEnv) -> list[AgentTool]:
        return []


module = VoiceModule()
