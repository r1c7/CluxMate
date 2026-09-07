"""Remember tool — record a durable cross-session fact (retrieval memory)."""

from typing import Any

from .base import BaseTool


class RememberTool(BaseTool):
    """Record a durable cross-session fact to retrieval memory."""

    def __init__(self, retrieval):
        self._retrieval = retrieval

    @property
    def name(self) -> str:
        return "remember"

    @property
    def description(self) -> str:
        return (
            "Record a durable, cross-session fact to retrieval memory. On future "
            "turns the most relevant remembered facts are automatically recalled "
            "and shown to you. Use for stable, reusable facts: environment "
            "specifics, decisions and their reasons, user preferences, gotchas. "
            "Default scope 'project' (this repo); use 'global' for facts that "
            "apply everywhere. To remove a fact, call forget with its id."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "Concise markdown fact; lead with the fact.",
                },
                "scope": {
                    "type": "string",
                    "enum": ["project", "global"],
                    "description": "'project' → this repo (default); 'global' → all projects.",
                },
            },
            "required": ["content"],
        }

    @property
    def risk_level(self) -> str:
        return "write"

    async def execute(self, content: str = "", scope: str = "project") -> str:
        return self._retrieval.remember(content, scope)
