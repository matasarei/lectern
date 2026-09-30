"""The OCR module: page images from PDFs and scans, and recognition through the `ocr` and
`ocr.hand` models. M2a gives it the engine (`pages`, `recognise`); the OCR session and its
tab come in M2b, and its agent tools (`ocr.start`, `ocr.note`) in M3."""

from __future__ import annotations

from lectern.agent.tools import AgentTool
from lectern.modules import ToolEnv


class OcrModule:
    id = "ocr"

    def tools(self, env: ToolEnv) -> list[AgentTool]:
        return []


module = OcrModule()
