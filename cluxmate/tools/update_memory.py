"""UpdateMemory tool — let the agent record durable learnings to AGENTS.md.

Appends a markdown entry to the project (`<cwd>/AGENTS.md`) or global
(`~/.cluxmate/AGENTS.md`) memory file. Memory is re-read each turn and injected
as a ``source:"memory"`` user message (see builder.injections_for_turn), so a
recorded entry is visible to the agent on the next message. To CORRECT or
remove an existing entry, edit AGENTS.md with search_replace — the full memory
text is already shown in the ``[Project memory]`` injection each turn.
"""

from typing import Any

from .base import BaseTool
from cluxmate.core.memory import MemoryManager


class UpdateMemoryTool(BaseTool):
    """Record a durable memory entry to project or global AGENTS.md."""

    def __init__(self, cwd: str):
        self._cwd = cwd

    @property
    def name(self) -> str:
        return "update_memory"

    @property
    def description(self) -> str:
        return (
            "Record a durable, cross-session learning to memory (AGENTS.md), "
            "which is injected as a [Project memory] message on future turns. Use for "
            "project conventions, build/test/run commands, non-obvious gotchas, "
            "and the 'why' behind architectural decisions — things not derivable "
            "by reading code or git history. Default scope is 'project' (this "
            "repo); use 'global' only for preferences that apply everywhere. To "
            "correct or delete an existing entry, edit AGENTS.md with "
            "search_replace instead — do not append a duplicate."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": (
                        "The memory entry as concise markdown. Include enough "
                        "context to be useful later; lead with the fact."
                    ),
                },
                "scope": {
                    "type": "string",
                    "enum": ["project", "global"],
                    "description": (
                        "'project' → <cwd>/AGENTS.md (default). "
                        "'global' → ~/.cluxmate/AGENTS.md (all projects)."
                    ),
                },
            },
            "required": ["content"],
        }

    @property
    def risk_level(self) -> str:
        return "write"

    async def execute(self, content: str = "", scope: str = "project") -> str:
        if not content.strip():
            return "Error: content is empty — nothing to record."
        if scope not in ("project", "global"):
            scope = "project"
        mgr = MemoryManager(self._cwd)
        try:
            path = mgr.append(content, scope)
        except Exception as e:
            return f"Error writing memory: {e}"
        msg = f"Recorded to {path} ({scope} memory)."
        if mgr.is_over_limit(scope):
            msg += (
                " Note: this memory file now exceeds the 32KB read cap and will "
                "be truncated when loaded — consider condensing it with "
                "search_replace."
            )
        return msg
