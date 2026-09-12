"""Base tool class, ToolBridge, and tool result types."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from ._output import bound_output

# Hard limit on tool output to avoid blowing up context window. Results above it
# keep their head and tail; the full text is spilled to
# <cwd>/.cluxmate/tmp-spill/ when the tool knows its working directory.
MAX_OUTPUT_CHARS = 40_000


@dataclass
class ToolResult:
    """Result of a single tool execution."""

    tool_call_id: str
    name: str
    content: str
    is_error: bool = False
    # Whole-value state payload for state-carrying tools: the agent loop
    # appends it to the session log as a log-only event (see
    # ``BaseTool.session_event``) after an executed, non-error call. Never
    # model-visible — the same posture as the audit fields (riskLevel/decision)
    # that ride ``tool/result`` events beside ``message``.
    data: dict[str, Any] | None = None


class BaseTool(ABC):
    """All tools inherit from this."""

    @property
    @abstractmethod
    def name(self) -> str: ...

    @property
    @abstractmethod
    def description(self) -> str: ...

    @property
    @abstractmethod
    def input_schema(self) -> dict[str, Any]: ...

    @property
    def risk_level(self) -> str:
        """Return 'safe', 'write', 'dangerous', or 'critical'. Override per tool."""
        return "safe"

    #: Log-only session event the agent loop appends (with the payload from
    #: :meth:`result_data`) after this tool executes successfully. None = this
    #: tool emits no session event. The event data must be whole-value: the
    #: complete post-call state, never a delta (see ``todo/write``).
    session_event: str | None = None

    @property
    def spill_cwd(self) -> str | None:
        """Working directory for spill artifacts (None → inline truncation only).

        Tools receive their workspace as ``_workdir`` or ``_cwd``; tools with no
        workspace (web_search, web_fetch) leave this None and simply get the
        bounded head/tail preview.
        """
        return getattr(self, "_workdir", None) or getattr(self, "_cwd", None)

    def result_data(self, args: dict[str, Any], output: str) -> dict[str, Any] | None:
        """Whole-value state payload for :attr:`session_event` (default: none).

        ``run_safe`` calls this only after a successful ``execute``; the
        default returns None, so ordinary tools emit no session event.
        Must be plain JSON (validated at log append) and must never raise
        for inputs that passed ``execute``."""
        return None

    @abstractmethod
    async def execute(self, **kwargs) -> str:
        """Execute the tool and return result text."""
        ...

    async def run_safe(self, tool_call_id: str, **kwargs) -> ToolResult:
        """Run execute() with error handling and output bounding."""
        try:
            output = await self.execute(**kwargs)
            if len(output) > MAX_OUTPUT_CHARS:
                output = bound_output(
                    output,
                    cwd=self.spill_cwd,
                    tool_name=self.name,
                    max_chars=MAX_OUTPUT_CHARS,
                )
            return ToolResult(
                tool_call_id=tool_call_id,
                name=self.name,
                content=output,
                data=self.result_data(kwargs, output),
            )
        except Exception as e:
            return ToolResult(
                tool_call_id=tool_call_id,
                name=self.name,
                content=f"Error: {e}",
                is_error=True,
            )

    def definition(self) -> dict[str, Any]:
        """Generate the API-facing tool definition."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


class ToolBridge:
    """Register tools and dispatch calls."""

    def __init__(self):
        self._tools: dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> None:
        self._tools[tool.name] = tool

    def tool(self, name: str) -> BaseTool | None:
        """Look up a registered tool by name (None when unknown)."""
        return self._tools.get(name)

    def definitions(self) -> list[dict[str, Any]]:
        return [t.definition() for t in self._tools.values()]

    async def call(
        self, name: str, params: dict[str, Any], tool_call_id: str = ""
    ) -> ToolResult:
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(
                tool_call_id=tool_call_id,
                name=name,
                content=f"Error: unknown tool '{name}'",
                is_error=True,
            )
        return await tool.run_safe(tool_call_id, **params)
