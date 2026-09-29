"""The contract every agent tool follows, core or module.

A tool has a dotted name (`project.find`) shown in the chat, and a wire name (`project_find`)
the model sees: OpenAI-style function names allow only letters, digits, `_` and `-`. Its
parameters are a pydantic model, which gives both the JSON schema the model gets and the
validation of what the model sends. A tool never returns more than `max_tokens`: a longer
result comes back as a page plus a cursor, and calling again with the cursor continues it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session, sessionmaker

from lectern.core.auth import User
from lectern.projects.store import Project

CHARS_PER_TOKEN = 3  # a rough, cautious estimate; the context builder counts real tokens
DEFAULT_MAX_TOKENS = 800


class ToolError(Exception):
    """A failure the model should see and can react to (unknown file, bad range…)."""


@dataclass
class ToolContext:
    project: Project
    sessions: sessionmaker[Session]  # the project's database
    user: User
    session_id: int  # the chat running the tool
    # How the chat is doing: "idle" | "running" | "waiting", by session id.
    turn_state: Callable[[int], str] = lambda _session_id: "idle"


@dataclass
class ToolResult:
    text: str
    # What the tool touched, for the chat's "In this chat" list:
    # {"kind": "artifact" | "session", "id": 3, "name": "…", "ref": "#c2"}
    refs: list[dict] = field(default_factory=list)
    cursor: str | None = None
    # Something for the page to do, such as {"type": "open_tab", "tab": {...}}.
    action: dict | None = None


@dataclass(frozen=True)
class AgentTool:
    name: str
    description: str
    params: type[BaseModel]
    run: Callable[[ToolContext, Any], ToolResult]  # sync; the loop runs it in a worker thread
    confirm: bool = False  # ask the person first
    max_tokens: int = DEFAULT_MAX_TOKENS
    # The question on the confirmation card, from the validated parameters.
    confirm_text: Callable[[Any], str] | None = None

    @property
    def wire_name(self) -> str:
        return self.name.replace(".", "_")

    def schema(self) -> dict:
        parameters = self.params.model_json_schema()
        parameters.pop("title", None)
        for prop in parameters.get("properties", {}).values():
            prop.pop("title", None)
        return {
            "type": "function",
            "function": {
                "name": self.wire_name,
                "description": self.description,
                "parameters": parameters,
            },
        }

    def validate(self, arguments: str | dict) -> BaseModel:
        """Parse the model's arguments. Raises ToolError with a message the model can use."""
        try:
            if isinstance(arguments, str):
                return self.params.model_validate_json(arguments or "{}")
            return self.params.model_validate(arguments)
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(str(p) for p in e['loc']) or 'arguments'}: {e['msg']}"
                for e in exc.errors()
            )
            raise ToolError(f"Invalid arguments for {self.name}: {problems}") from None


def page(text: str, cursor: str | None, max_tokens: int) -> tuple[str, str | None]:
    """One page of `text` within `max_tokens`, cut at a line break where possible, and the
    cursor for the rest (None when this is the last page)."""
    try:
        start = int(cursor) if cursor else 0
    except ValueError:
        raise ToolError(f"Unknown cursor {cursor!r}; call again without one.") from None
    if start < 0 or start > len(text):
        raise ToolError(f"Cursor {cursor!r} is past the end; call again without one.")
    budget = max_tokens * CHARS_PER_TOKEN
    end = start + budget
    if end >= len(text):
        return text[start:], None
    cut = text.rfind("\n", start + budget // 2, end)
    end = cut + 1 if cut != -1 else end
    remaining = len(text) - end
    return (
        f'{text[start:end]}\n[{remaining} more characters: call again with cursor="{end}"]',
        str(end),
    )


class Registry:
    def __init__(self, tools: list[AgentTool]) -> None:
        self._by_wire = {t.wire_name: t for t in tools}
        if len(self._by_wire) != len(tools):
            raise ValueError("Two tools share a wire name.")

    def all(self) -> list[AgentTool]:
        return list(self._by_wire.values())

    def get(self, wire_name: str) -> AgentTool | None:
        return self._by_wire.get(wire_name)

    def schemas(self) -> list[dict]:
        return [t.schema() for t in self._by_wire.values()]
