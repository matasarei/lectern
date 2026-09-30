"""The OCR module: page images from PDFs and scans, recognition through the `ocr` and
`ocr.hand` models, and OCR sessions (`store`, `jobs`, and their API). Its agent tools
(`ocr.start`, `ocr.note`) come in M3."""

from __future__ import annotations

from lectern.agent.tools import AgentTool
from lectern.modules import ToolEnv
from lectern.modules.ocr import api


class OcrModule:
    id = "ocr"
    router = api.router  # /api/projects/{id}/ocr/…

    def tools(self, env: ToolEnv) -> list[AgentTool]:
        return []


module = OcrModule()
