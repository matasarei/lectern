"""Image generation: pictures from a prompt, made on this computer by stable-diffusion.cpp
(generate.py), asked for by the chat (tools.py: `image.generate`)."""

from __future__ import annotations

from lectern.agent.tools import AgentTool
from lectern.modules import ToolEnv
from lectern.modules.image import tools


class ImageModule:
    id = "image"

    def tools(self, env: ToolEnv) -> list[AgentTool]:
        return [tools.GENERATE]


module = ImageModule()
