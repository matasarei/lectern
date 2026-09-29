"""The Sandbox module: scripts written by the coding model, run in a locked-down container
(Podman or Docker). Its tools are offered only while a container engine is running."""

from __future__ import annotations

from lectern.agent.tools import AgentTool
from lectern.modules import ToolEnv


class SandboxModule:
    id = "sandbox"

    def tools(self, env: ToolEnv) -> list[AgentTool]:
        if not env.sandbox:
            return []
        return []  # code.write and code.run arrive in the next steps


module = SandboxModule()
