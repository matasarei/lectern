"""PDF editing: change a PDF's text in place, keeping its fonts, sizes and layout, or fill its
form (docs/agent.md, "Tools"). The engine and why it is built on PDFium:
docs/acceptance/pdf-edit.md."""

from __future__ import annotations

from lectern.agent.tools import AgentTool
from lectern.modules import ToolEnv
from lectern.modules.pdf_edit.tools import pdf_tools


class PdfEditModule:
    id = "pdf-edit"

    def tools(self, env: ToolEnv) -> list[AgentTool]:
        return pdf_tools()


module = PdfEditModule()
