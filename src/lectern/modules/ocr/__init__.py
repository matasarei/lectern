"""The OCR module: page images from PDFs and scans, recognition through the `ocr` and
`ocr.hand` models, OCR sessions (`store`, `jobs`, and their API), and its chat tools
(`ocr.start`, `ocr.note`)."""

from __future__ import annotations

from lectern.agent.tools import AgentTool
from lectern.modules import ToolEnv
from lectern.modules.ocr import api, tools


class OcrModule:
    id = "ocr"
    router = api.router  # /api/projects/{id}/ocr/…

    def tools(self, env: ToolEnv) -> list[AgentTool]:
        return [tools.START, tools.NOTE]


module = OcrModule()
