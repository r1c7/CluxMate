"""Forget tool — delete a remembered fact by id (retrieval memory)."""

from typing import Any

from .base import BaseTool


class ForgetTool(BaseTool):
    """Delete a remembered fact by id (shown on recalled facts)."""

    def __init__(self, retrieval):
        self._retrieval = retrieval

    @property
    def name(self) -> str:
        return "forget"

    @property
    def description(self) -> str:
        return (
            "Delete a retrieval-memory fact by its id (the id shown next to a "
            "recalled fact). Use only to correct or remove a fact that is wrong "
            "or no longer useful. Removes retrieval facts only, NOT AGENTS.md "
            "entries — edit those with search_replace (see update_memory)."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "fact_id": {
                    "type": "string",
                    "description": "The fact id to delete.",
                },
            },
            "required": ["fact_id"],
        }

    @property
    def risk_level(self) -> str:
        return "write"

    async def execute(self, fact_id: str = "") -> str:
        return self._retrieval.forget(fact_id)
